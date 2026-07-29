"""KoeLog - 会議録音 文字起こしツール"""

import os
import uuid
import threading
import subprocess
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from faster_whisper import WhisperModel

import json

import config

# --- 設定ファイル ---
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")


def load_settings():
    """settings.jsonから設定を読み込む。なければconfig.pyのデフォルト値"""
    defaults = {
        "smtp_host": config.SMTP_HOST,
        "smtp_port": config.SMTP_PORT,
        "smtp_user": config.SMTP_USER,
        "smtp_password": config.SMTP_PASSWORD,
        "smtp_from": config.SMTP_FROM,
        "smtp_use_tls": config.SMTP_USE_TLS,
        "mail_enabled": config.MAIL_ENABLED,
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            defaults.update(saved)
        except Exception:
            pass
    return defaults


def save_settings(data: dict):
    """settings.jsonに設定を保存"""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --- ディレクトリ作成 ---
os.makedirs(config.UPLOAD_DIR, exist_ok=True)
os.makedirs(config.RESULT_DIR, exist_ok=True)

# --- モデルロード（起動時に1回だけ） ---
print(f"[KoeLog] Whisperモデル読み込み中... (size={config.MODEL_SIZE}, device={config.DEVICE}, compute_type={config.COMPUTE_TYPE})")
model = WhisperModel(config.MODEL_SIZE, device=config.DEVICE, compute_type=config.COMPUTE_TYPE)
print("[KoeLog] モデル読み込み完了")

# --- FastAPI ---
app = FastAPI(title="KoeLog")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# --- ジョブ管理 ---
jobs: dict = {}

# --- 同時処理制御（CPUリソース保護） ---
processing_semaphore = threading.Semaphore(1)


def format_timestamp(seconds: float) -> str:
    """秒数を HH:MM:SS 形式に変換"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_srt_timestamp(seconds: float) -> str:
    """SRT形式のタイムスタンプ（HH:MM:SS,mmm）"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_result(segments_data, output_format):
    """出力形式に応じたテキストを生成"""
    if output_format == "plain":
        # プレーンテキスト（タイムスタンプなし）
        return "\n".join(seg["text"] for seg in segments_data)
    elif output_format == "srt":
        # SRT字幕形式
        lines = []
        for i, seg in enumerate(segments_data, 1):
            start_srt = format_srt_timestamp(seg["start"])
            end_srt = format_srt_timestamp(seg["end"])
            lines.append(str(i))
            lines.append(f"{start_srt} --> {end_srt}")
            lines.append(seg["text"])
            lines.append("")
        return "\n".join(lines)
    else:
        # タイムスタンプ付き（デフォルト）
        return "\n".join(f"[{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}] {seg['text']}" for seg in segments_data)


def preprocess_audio(input_path: str) -> str:
    """ffmpegで16kHz/モノラル/ノーマライズに変換"""
    output_path = input_path + ".preprocessed.wav"
    try:
        cmd = [
            config.FFMPEG_PATH, "-y", "-i", input_path,
            "-ar", "16000",      # 16kHzリサンプリング
            "-ac", "1",          # モノラル
            "-af", "loudnorm",   # ラウドネスノーマライズ
            "-f", "wav",
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and os.path.exists(output_path):
            return output_path
        else:
            print(f"[KoeLog] ffmpeg前処理失敗（元ファイルを使用）: {result.stderr[:200]}")
            return input_path
    except FileNotFoundError:
        print("[KoeLog] ffmpegが見つかりません。前処理をスキップします。")
        return input_path
    except Exception as e:
        print(f"[KoeLog] ffmpeg前処理エラー: {e}")
        return input_path


def get_audio_duration(filepath: str) -> float:
    """ffprobeで音声の長さ（秒）を取得"""
    try:
        cmd = [
            config.FFMPEG_PATH.replace("ffmpeg", "ffprobe"),
            "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as e:
        print(f"[KoeLog] ffprobe error: {e}")
    return 0.0


def send_notification(to_email: str, job: dict):
    """完了通知メールを送信"""
    settings = load_settings()
    if not settings.get("mail_enabled") or not to_email:
        return
    try:
        subject = f"[KoeLog] 文字起こし完了: {job['filename']}"

        # 結果テキストの冒頭を抜粋
        preview = ""
        if job.get("result_path") and os.path.exists(job["result_path"]):
            with open(job["result_path"], "r", encoding="utf-8-sig") as f:
                all_lines = f.readlines()
                preview = "".join(all_lines[:10])
                if len(all_lines) > 10:
                    preview += "\n... (以下省略)"

        body = f"""文字起こしが完了しました。

ファイル名: {job['filename']}
サイズ: {job.get('file_size_mb', '?')} MB

--- 結果プレビュー（冒頭10行） ---
{preview}

---
結果のダウンロードはKoeLogの画面から行ってください。
"""

        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = settings.get("smtp_from") or settings.get("smtp_user")
        msg["To"] = to_email
        msg.attach(MIMEText(body, "plain", "utf-8"))

        if settings.get("smtp_use_tls"):
            server = smtplib.SMTP(settings["smtp_host"], settings["smtp_port"])
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings["smtp_host"], settings["smtp_port"])

        server.login(settings["smtp_user"], settings["smtp_password"])
        server.send_message(msg)
        server.quit()
        print(f"[KoeLog] 通知メール送信完了: {to_email}")
    except Exception as e:
        print(f"[KoeLog] メール送信エラー: {e}")


def transcribe_worker(job_id: str, filepath: str):
    """バックグラウンドで文字起こしを実行"""
    job = jobs[job_id]
    processing_semaphore.acquire()
    job["status"] = "processing"

    preprocessed_path = None
    try:
        # ffmpeg前処理
        preprocessed_path = preprocess_audio(filepath)

        segments, info = model.transcribe(
            preprocessed_path,  # 前処理済みファイルを使用
            language=config.LANGUAGE,
            beam_size=5,
            vad_filter=True,
            initial_prompt=config.INITIAL_PROMPT,
            temperature=config.TEMPERATURE,
        )

        # セグメントデータを収集
        segments_data = []
        for segment in segments:
            segments_data.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
            })

        # 出力形式に応じたテキスト生成
        output_format = job.get("output_format", "timestamp")
        text = format_result(segments_data, output_format)

        ext = ".srt" if output_format == "srt" else ".txt"
        result_path = os.path.join(config.RESULT_DIR, f"{job_id}{ext}")

        # BOM付きUTF-8で保存
        with open(result_path, "w", encoding="utf-8-sig") as f:
            f.write(text)

        job["status"] = "completed"
        job["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job["result_path"] = result_path

        # メール通知
        send_notification(job.get("email", ""), job)

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    finally:
        # 前処理ファイルを削除
        if preprocessed_path and preprocessed_path != filepath and os.path.exists(preprocessed_path):
            try:
                os.remove(preprocessed_path)
            except OSError:
                pass
        processing_semaphore.release()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """メイン画面"""
    job_list = sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "jobs": job_list,
        "max_file_size_mb": config.MAX_FILE_SIZE_MB,
        "allowed_extensions": ", ".join(sorted(config.ALLOWED_EXTENSIONS)),
    })


@app.post("/upload")
async def upload(file: UploadFile = File(...), email: str = Form(""), output_format: str = Form("timestamp")):
    """音声ファイルアップロード → ジョブ作成"""
    # 拡張子チェック
    _, ext = os.path.splitext(file.filename or "")
    ext = ext.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"非対応のファイル形式です。対応形式: {', '.join(sorted(config.ALLOWED_EXTENSIONS))}"
        )

    # ファイル読み込み
    contents = await file.read()

    # サイズチェック
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > config.MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"ファイルサイズが上限({config.MAX_FILE_SIZE_MB}MB)を超えています。({size_mb:.1f}MB)"
        )

    # ジョブ作成
    job_id = str(uuid.uuid4())
    safe_filename = f"{job_id}{ext}"
    filepath = os.path.join(config.UPLOAD_DIR, safe_filename)

    with open(filepath, "wb") as f:
        f.write(contents)

    # 音声の長さを取得
    duration = get_audio_duration(filepath)

    jobs[job_id] = {
        "job_id": job_id,
        "filename": file.filename,
        "status": "waiting",
        "progress": 0,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at": None,
        "result_path": None,
        "error": None,
        "upload_path": filepath,
        "file_size_mb": round(size_mb, 1),
        "email": email.strip(),
        "output_format": output_format,
        "duration_sec": round(duration, 1),
        "estimated_sec": round(duration * 0.7, 0),
    }

    # バックグラウンドで文字起こし開始
    thread = threading.Thread(target=transcribe_worker, args=(job_id, filepath), daemon=True)
    thread.start()

    return JSONResponse(content={"job_id": job_id, "status": "ok"})


@app.get("/status")
async def status():
    """ジョブ一覧JSON"""
    job_list = sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)
    # result_path, upload_path はフロントに渡さない
    safe_list = []
    for j in job_list:
        safe_list.append({
            "job_id": j["job_id"],
            "filename": j["filename"],
            "status": j["status"],
            "created_at": j["created_at"],
            "completed_at": j["completed_at"],
            "error": j["error"],
            "file_size_mb": j.get("file_size_mb", 0),
            "duration_sec": j.get("duration_sec", 0),
            "estimated_sec": j.get("estimated_sec", 0),
        })
    return JSONResponse(content=safe_list)


@app.get("/download/{job_id}")
async def download(job_id: str):
    """結果TXTダウンロード"""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    if job["status"] != "completed" or not job["result_path"]:
        raise HTTPException(status_code=400, detail="まだ完了していません")
    if not os.path.exists(job["result_path"]):
        raise HTTPException(status_code=404, detail="結果ファイルが見つかりません")

    # ダウンロード用ファイル名: 元ファイル名.txt or .srt
    original_name = os.path.splitext(job["filename"] or "result")[0]
    ext = ".srt" if job.get("output_format") == "srt" else ".txt"
    download_name = f"{original_name}{ext}"

    return FileResponse(
        path=job["result_path"],
        filename=download_name,
        media_type="text/plain; charset=utf-8",
    )


@app.get("/preview/{job_id}")
async def preview(job_id: str):
    """結果テキストをJSONで返す"""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    if job["status"] != "completed" or not job["result_path"]:
        raise HTTPException(status_code=400, detail="まだ完了していません")
    if not os.path.exists(job["result_path"]):
        raise HTTPException(status_code=404, detail="結果ファイルが見つかりません")
    with open(job["result_path"], "r", encoding="utf-8-sig") as f:
        text = f.read()
    return JSONResponse(content={"text": text, "filename": job["filename"]})


@app.post("/delete/{job_id}")
async def delete(job_id: str):
    """ジョブ削除"""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    if job["status"] == "processing":
        raise HTTPException(status_code=400, detail="処理中のジョブは削除できません")

    # アップロードファイル削除
    upload_path = job.get("upload_path")
    if upload_path and os.path.exists(upload_path):
        try:
            os.remove(upload_path)
        except OSError:
            pass

    # 結果ファイル削除
    result_path = job.get("result_path")
    if result_path and os.path.exists(result_path):
        try:
            os.remove(result_path)
        except OSError:
            pass

    del jobs[job_id]

    return RedirectResponse(url="/", status_code=303)


# --- 管理画面 ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """管理画面"""
    settings = load_settings()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "settings": settings,
    })


@app.post("/admin/save")
async def admin_save(
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_use_tls: str = Form("on"),
    mail_enabled: str = Form("off"),
):
    """SMTP設定を保存"""
    data = {
        "smtp_host": smtp_host.strip(),
        "smtp_port": smtp_port,
        "smtp_user": smtp_user.strip(),
        "smtp_password": smtp_password.strip(),
        "smtp_from": smtp_from.strip(),
        "smtp_use_tls": smtp_use_tls == "on",
        "mail_enabled": mail_enabled == "on",
    }
    save_settings(data)
    return JSONResponse(content={"status": "ok"})


@app.post("/admin/test")
async def admin_test(test_email: str = Form("")):
    """テストメール送信"""
    if not test_email.strip():
        return JSONResponse(content={"status": "error", "message": "メールアドレスを入力してください"})
    settings = load_settings()
    if not settings.get("mail_enabled"):
        return JSONResponse(content={"status": "error", "message": "メール通知が無効です。先に設定を保存して有効にしてください"})
    try:
        msg = MIMEMultipart()
        msg["Subject"] = "[KoeLog] テストメール"
        msg["From"] = settings.get("smtp_from") or settings.get("smtp_user")
        msg["To"] = test_email.strip()
        msg.attach(MIMEText("KoeLogのメール通知テストです。\nこのメールが届いていれば設定は正しく完了しています。", "plain", "utf-8"))

        if settings.get("smtp_use_tls"):
            server = smtplib.SMTP(settings["smtp_host"], settings["smtp_port"])
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings["smtp_host"], settings["smtp_port"])
        server.login(settings["smtp_user"], settings["smtp_password"])
        server.send_message(msg)
        server.quit()
        return JSONResponse(content={"status": "ok", "message": "テストメールを送信しました"})
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": f"送信失敗: {str(e)}"})
