"""KoeLog - 会議録音 文字起こしツール"""

import os
import uuid
import threading
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from faster_whisper import WhisperModel

import config

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


def transcribe_worker(job_id: str, filepath: str):
    """バックグラウンドで文字起こしを実行"""
    job = jobs[job_id]
    processing_semaphore.acquire()
    job["status"] = "processing"

    try:
        segments, info = model.transcribe(
            filepath,
            language=config.LANGUAGE,
            beam_size=5,
            vad_filter=True,
        )

        result_path = os.path.join(config.RESULT_DIR, f"{job_id}.txt")
        lines = []
        for segment in segments:
            start_ts = format_timestamp(segment.start)
            end_ts = format_timestamp(segment.end)
            line = f"[{start_ts} --> {end_ts}] {segment.text.strip()}"
            lines.append(line)

        text = "\n".join(lines)

        # BOM付きUTF-8で保存
        with open(result_path, "w", encoding="utf-8-sig") as f:
            f.write(text)

        job["status"] = "completed"
        job["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job["result_path"] = result_path

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    finally:
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
async def upload(file: UploadFile = File(...)):
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

    # ダウンロード用ファイル名: 元ファイル名.txt
    original_name = os.path.splitext(job["filename"] or "result")[0]
    download_name = f"{original_name}.txt"

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
