"""KoeLog - 会議録音 文字起こしツール"""

import os
import io
import csv
import gc
import time
import uuid
import queue
import threading
import subprocess
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import json

import config
import presets

# --- 検証用モック（KOELOG_MOCK=1 のときだけ faster-whisper を使わない） ---
MOCK_MODE = os.environ.get("KOELOG_MOCK") == "1"
if not MOCK_MODE:
    from faster_whisper import WhisperModel

# --- 設定ファイル ---
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")
DICTIONARY_FILE = os.path.join(os.path.dirname(__file__), "dictionary.json")


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
        # 保存先（空欄なら config.UPLOAD_DIR / config.RESULT_DIR）
        "upload_dir": "",
        "result_dir": "",
        # 精度と時間のバランス（既定の段階）
        "default_quality_level": presets.DEFAULT_LEVEL,
        # 段階ごとの実測倍率（このサーバーでの実績。推定処理時間の精度を上げる）
        "quality_stats": {},
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


def update_settings(partial: dict):
    """settings.json の一部だけ更新（他セクションの設定を消さない）"""
    current = load_settings()
    current.update(partial)
    save_settings(current)
    return current


# --- 保存先ディレクトリ ---

def validate_storage_path(path: str):
    """保存先パスの検証。(ok, message) を返す。空欄は既定扱いで ok"""
    p = (path or "").strip()
    if not p:
        return True, ""
    if any(ord(c) < 32 for c in p):
        return False, "制御文字を含むパスは指定できません"
    if not os.path.isabs(p):
        return False, "絶対パスで指定してください（相対パスは不可）"
    segments = p.replace("/", "\\").split("\\")
    if ".." in segments:
        return False, "「..」を含むパスは指定できません"
    return True, ""


def get_upload_dir() -> str:
    """現在のアップロード先（絶対パス）"""
    s = load_settings()
    p = (s.get("upload_dir") or "").strip()
    ok, _ = validate_storage_path(p)
    if not p or not ok:
        p = config.UPLOAD_DIR
    return os.path.abspath(p)


def get_default_quality_level() -> str:
    """管理画面で設定された既定の段階（不正値は presets 側で既定に落ちる）"""
    return presets.normalize_level(load_settings().get("default_quality_level"))


def get_quality_stats() -> dict:
    """段階ごとの所要時間の実績（このサーバーでの実測倍率）"""
    s = load_settings().get("quality_stats")
    return s if isinstance(s, dict) else {}


def record_quality_stat(level: str, duration_sec: float, elapsed_sec: float):
    """1本終わるごとに倍率の実績を更新する。

    組み込みの見込みは検証用PCからの換算なので、実機では必ずずれる。
    実績がたまったら推定処理時間の表示をそちらに切り替える。
    モック時は実時間に意味がないので呼ばない。
    """
    try:
        stats = presets.update_stats(get_quality_stats(), level, duration_sec, elapsed_sec)
        update_settings({"quality_stats": stats})
    except Exception as e:
        # 統計の更新に失敗してもジョブ自体は成功扱いのままにする
        print(f"[KoeLog] 所要時間の実績の記録に失敗しました: {e}")


def get_result_dir() -> str:
    """現在の結果保存先（絶対パス）"""
    s = load_settings()
    p = (s.get("result_dir") or "").strip()
    ok, _ = validate_storage_path(p)
    if not p or not ok:
        p = config.RESULT_DIR
    return os.path.abspath(p)


def test_storage_path(path: str, create: bool = False):
    """指定パスへ書き込みテスト。dict(status, message) を返す"""
    ok, msg = validate_storage_path(path)
    p = (path or "").strip()
    if not p:
        return {"status": "ok", "message": "空欄のため既定のフォルダを使用します"}
    if not ok:
        return {"status": "error", "message": msg}
    if not os.path.isdir(p):
        if os.path.exists(p):
            return {"status": "error", "message": "ファイルが存在します（フォルダではありません）"}
        if not create:
            return {"status": "missing", "message": "フォルダが存在しません"}
        try:
            os.makedirs(p, exist_ok=True)
        except PermissionError:
            return {"status": "error", "message": "権限がないため作成できません"}
        except OSError as e:
            return {"status": "error", "message": f"作成できません: {e.strerror or e}"}
    probe = os.path.join(p, f".koelog_write_test_{uuid.uuid4().hex}")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("test")
        os.remove(probe)
    except PermissionError:
        return {"status": "error", "message": "権限がありません（書き込み不可）"}
    except OSError as e:
        return {"status": "error", "message": f"書き込みできません: {e.strerror or e}"}
    return {"status": "ok", "message": "OK（書き込み可能）"}


# --- Custom Dictionary ---

def load_dictionary():
    """Load custom dictionary entries"""
    if not os.path.exists(DICTIONARY_FILE):
        return []
    try:
        with open(DICTIONARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", [])
    except Exception:
        return []


def save_dictionary(entries):
    """Save custom dictionary entries"""
    data = {"version": 1, "entries": entries}
    with open(DICTIONARY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def apply_dictionary(text):
    """Apply dictionary replacements to text"""
    entries = load_dictionary()
    for entry in entries:
        if entry.get("from") and entry.get("to"):
            text = text.replace(entry["from"], entry["to"])
    return text


# --- ディレクトリ作成 ---
os.makedirs(config.UPLOAD_DIR, exist_ok=True)
os.makedirs(config.RESULT_DIR, exist_ok=True)

# --- モデルロード ---
# 段階ごとにモデルが違うので、使うときに読み込んで保持する。
# 既定では1個だけ保持する（16GBのサーバーで medium と large-v3 を
# 同時に抱えるとメモリが足りなくなるため）。
MODEL_CACHE_SIZE = max(1, int(getattr(config, "MODEL_CACHE_SIZE", 1)))
_model_cache: "dict[tuple, object]" = {}

# ロックは2本に分ける。
#   _cache_lock : キャッシュ辞書を触るあいだだけ握る「短いロック」。
#                 状態の読み取り（管理画面）もこちらしか使わない。
#   _load_lock  : 実際にモデルを構築するあいだ握る「長いロック」。
#                 大きいモデルを2つ同時に読み込んでメモリを倍使わないため。
# 1本にまとめると、読み込み（初回は約3.1GBのダウンロード）のあいだ
# 管理画面の「モデルの状態を再確認」が返らなくなる。
# 実測では読み込み20秒のうち19.1秒待たされていた。
_cache_lock = threading.Lock()
_load_lock = threading.Lock()
# 読み込み中のキー -> 完了を知らせるイベント。
# 同じモデルを複数のスレッドが同時に要求しても、読み込むのは1回だけにする。
_loading: "dict[tuple, threading.Event]" = {}


def _evict_for_new_model():
    """新しいモデルを読む前に、保持数を下回るまで古いものを捨てる。

    読み込みの**前**に捨てる（後ではない）。後に捨てると、入れ替えの瞬間だけ
    2個ぶんのメモリを使う。16GB のサーバーで medium と large-v3 が
    重なると苦しいので、ピークを増やさない。
    """
    with _cache_lock:
        while len(_model_cache) >= MODEL_CACHE_SIZE:
            old = next(iter(_model_cache))
            print(f"[KoeLog] モデルを解放します: {old}")
            del _model_cache[old]
            gc.collect()


def get_model(model_size: str, compute_type: str, cpu_threads: int = 0):
    """指定の構成のモデルを返す。未読込なら読み込む（初回はダウンロードが走る）"""
    key = (model_size, compute_type, int(cpu_threads or 0))
    while True:
        with _cache_lock:
            if key in _model_cache:
                return _model_cache[key]
            ev = _loading.get(key)
            if ev is None:
                # 自分が読み込む係になる
                ev = threading.Event()
                _loading[key] = ev
                leader = True
            else:
                leader = False
        if leader:
            break
        # 別のスレッドが同じモデルを読み込み中。終わるのを待つ（ロックは握らない）
        ev.wait()
        # 読み込めていればキャッシュから取れる。失敗していたら自分が係になってやり直す

    try:
        # 「退避 → 構築 → 挿入」を _load_lock の中でひとまとまりにする。
        #
        # 退避や挿入をこの外に出すと、別モデルの係が「相手が挿入する前」に
        # 退避してしまい、捨てるものが無いまま自分のぶんを足すので
        # 保持数が MODEL_CACHE_SIZE を超える。
        # 実測（Barrier で4本同時・差し替えた置物）: 外に出していたときは
        # 4個とも残った（['base','large-v3','medium','small']）。
        #
        # ロックの順序は必ず _load_lock → _cache_lock。逆順で取る場所を
        # 作らないこと（作るとデッドロックする）。
        # 退避は構築の**前**のままなので、メモリのピークは増えない。
        with _load_lock:
            _evict_for_new_model()
            print(f"[KoeLog] Whisperモデル読み込み中... "
                  f"(size={model_size}, device={config.DEVICE}, "
                  f"compute_type={compute_type}, cpu_threads={key[2]})")
            t0 = time.time()
            # 構築中は _cache_lock を握らない。握ると状態の読み取りまで止まる
            m = WhisperModel(model_size, device=config.DEVICE,
                             compute_type=compute_type, cpu_threads=key[2])
            print(f"[KoeLog] モデル読み込み完了 ({time.time() - t0:.1f}秒)")
            with _cache_lock:
                _model_cache[key] = m
        return m
    finally:
        with _cache_lock:
            _loading.pop(key, None)
        ev.set()


def loaded_model_keys():
    """いま読み込まれているモデル構成（管理画面の表示用）。

    読み込み中でも即座に返す（短いロックしか取らない）。
    """
    with _cache_lock:
        keys = list(_model_cache)
    return [{"model_size": k[0], "compute_type": k[1], "cpu_threads": k[2]}
            for k in keys]


def loading_model_keys():
    """いま読み込み中のモデル構成（管理画面の表示用）"""
    with _cache_lock:
        keys = list(_loading)
    return [{"model_size": k[0], "compute_type": k[1], "cpu_threads": k[2]}
            for k in keys]


def _warm_default_model():
    """既定段階のモデルを裏で先読みする（起動を待たせない）"""
    p = presets.get(get_default_quality_level())
    try:
        get_model(p["model_size"], p["compute_type"], p.get("cpu_threads", 0))
    except Exception as e:
        # ここで落としても起動は続ける。ジョブ実行時に読み直す
        print(f"[KoeLog] 起動時のモデル先読みに失敗しました（ジョブ実行時に再試行します）: {e}")


if MOCK_MODE:
    print("[KoeLog] KOELOG_MOCK=1: Whisperモデルは読み込みません（検証用ダミー処理）")
else:
    # 既定段階のモデルを**別スレッドで**先読みする。
    #
    # ここで同期的に読むと、import が終わるまで uvicorn が listen できず、
    # 読み込みの間ずっと「サーバーが無応答」に見える。
    # 段階5(large-v3)を既定にすると、未取得なら約3.1GBのダウンロードぶん、
    # 取得済みでも読み込みぶん（検証PCで149秒）が丸ごと無応答時間になっていた。
    # 先に listen し、モデルは裏で用意する。
    # 用意できる前にジョブが来ても、get_model が同じロックで待つので二重には読まない。
    threading.Thread(target=_warm_default_model, daemon=True,
                     name="koelog-model-warmup").start()
    print("[KoeLog] 既定段階のモデルを裏で読み込み中です（画面はすぐ使えます）")

# --- FastAPI ---
app = FastAPI(title="KoeLog")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# --- ジョブ管理 ---
jobs: dict = {}
jobs_lock = threading.Lock()
_job_seq = 0  # 受付順（同一秒内でも順序を保つ）

# --- FIFOキュー＋ワーカー（受付順に処理。CPUリソース保護のため既定1本） ---
WORKER_COUNT = max(1, int(getattr(config, "WORKER_COUNT", 1)))
job_queue: "queue.Queue[str]" = queue.Queue()


def next_seq() -> int:
    global _job_seq
    _job_seq += 1
    return _job_seq


def waiting_job_ids_in_order():
    """待機中ジョブIDを受付順で返す"""
    with jobs_lock:
        waiting = [j for j in jobs.values() if j["status"] == "waiting"]
    waiting.sort(key=lambda j: j["seq"])
    return [j["job_id"] for j in waiting]


def build_job_view(job: dict, position_map: dict) -> dict:
    """フロントに渡す安全なジョブ表現（パス類は含めない）"""
    return {
        "job_id": job["job_id"],
        "filename": job["filename"],
        "status": job["status"],
        "queue_position": position_map.get(job["job_id"], 0),
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "completed_at": job["completed_at"],
        "error": job["error"],
        "file_size_mb": job.get("file_size_mb", 0),
        "duration_sec": job.get("duration_sec", 0),
        "estimated_sec": job.get("estimated_sec", 0),
        "username": job.get("username", "anonymous"),
        "quality_level": job.get("quality_level", presets.DEFAULT_LEVEL),
        "quality_name": presets.get(job.get("quality_level"))["name"],
        # 処理中の内訳。モデルの用意（初回はダウンロード）は時間がかかるので区別する
        "phase": job.get("phase", ""),
        # 実際にかかった時間の内訳。モデルの用意は音声の長さに比例しない固定費なので
        # 分けて持つ（実績の倍率は transcribe_sec のほうで計算している）
        "elapsed_sec": job.get("elapsed_sec", 0),
        "model_load_sec": job.get("model_load_sec", 0),
        "transcribe_sec": job.get("transcribe_sec", 0),
    }


def build_job_list():
    """ジョブ一覧（新しい順、待機中には queue_position 付き）"""
    order = waiting_job_ids_in_order()
    position_map = {jid: i + 1 for i, jid in enumerate(order)}
    with jobs_lock:
        job_list = sorted(jobs.values(), key=lambda j: j["seq"], reverse=True)
        return [build_job_view(j, position_map) for j in job_list]


def remove_job_files(job: dict):
    """ジョブに紐づくファイルを削除"""
    for key in ("upload_path", "result_path"):
        p = job.get(key)
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


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


def run_transcription(job: dict, filepath: str):
    """1ジョブの文字起こし本体。結果テキストを返す"""
    if MOCK_MODE:
        # 検証用: 数秒待ってダミー結果を返す（faster-whisper / ffmpeg 不要）
        time.sleep(float(os.environ.get("KOELOG_MOCK_SLEEP", "3")))
        lv = presets.normalize_level(job.get("quality_level"))
        p = presets.get(lv)
        segments_data = [
            {"start": 0.0, "end": 2.5, "text": f"ダミー文字起こし: {job['filename']}"},
            {"start": 2.5, "end": 5.0, "text": "KOELOG_MOCK=1 で生成された検証用テキストです。"},
            # どの段階で動いたかを検証できるようにする（モック時のみ）
            {"start": 5.0, "end": 7.5,
             "text": f"段階={lv} ({p['name']}) model={p['model_size']} "
                     f"beam={p['transcribe'].get('beam_size')} "
                     f"vad={p['transcribe'].get('vad_filter')}"},
        ]
        return format_result(segments_data, job.get("output_format", "timestamp"))

    # 分野別プロンプト選択
    field = job.get("field", "general")
    if field == "custom" and job.get("custom_prompt"):
        prompt = job["custom_prompt"]
    elif field in config.PROMPT_PRESETS:
        prompt = config.PROMPT_PRESETS[field]
    else:
        prompt = config.INITIAL_PROMPT

    # 精度と時間のバランス（段階）。ジョブ受付時に確定した値を使う。
    # 段階3 は従来の呼び出しと同じ引数になる（presets.py 参照）
    level = presets.normalize_level(job.get("quality_level"))
    preset = presets.get(level)

    # モデルの用意にかかった時間は、音声の長さに比例しない「固定費」。
    # 所要時間の実績（倍率）に混ぜると倍率が膨らむので、別に測って後で差し引く。
    # 初回はここでダウンロード（large-v3 で約3.1GB）が走るため、
    # 画面に「モデルを準備中」と出せるよう phase を立てる。
    job["phase"] = "model"
    _t_model = time.time()
    try:
        model = get_model(preset["model_size"], preset["compute_type"],
                          preset.get("cpu_threads", 0))
    finally:
        job["model_load_sec"] = round(time.time() - _t_model, 1)
        job["phase"] = "transcribe"

    segments, info = model.transcribe(
        filepath,  # 前処理済みファイルを使用
        language=config.LANGUAGE,
        initial_prompt=prompt,
        **preset["transcribe"],
    )

    # セグメントデータを収集
    segments_data = []
    for segment in segments:
        segments_data.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })

    return format_result(segments_data, job.get("output_format", "timestamp"))


def process_job(job_id: str):
    """1ジョブを処理（ワーカースレッドから呼ばれる）"""
    with jobs_lock:
        job = jobs.get(job_id)
        # 取消・削除済みなら何もしない
        if not job or job["status"] != "waiting":
            return
        job["status"] = "processing"
        job["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[KoeLog] 処理開始 {job['started_at']} job={job_id} file={job['filename']}")

    filepath = job["upload_path"]
    preprocessed_path = None
    t_start = time.time()   # 所要時間の実測（前処理＋文字起こし）
    try:
        # ffmpeg前処理（モック時はスキップ）
        preprocessed_path = filepath if MOCK_MODE else preprocess_audio(filepath)

        text = run_transcription(job, preprocessed_path)

        # Apply custom dictionary post-processing
        text = apply_dictionary(text)

        output_format = job.get("output_format", "timestamp")
        ext = ".srt" if output_format == "srt" else ".txt"
        # 結果保存先はジョブ受付時に確定したもの（設定変更は次のジョブから有効）
        result_dir = job.get("result_dir") or os.path.abspath(config.RESULT_DIR)
        os.makedirs(result_dir, exist_ok=True)
        result_path = os.path.join(result_dir, f"{job_id}{ext}")

        # BOM付きUTF-8で保存
        with open(result_path, "w", encoding="utf-8-sig") as f:
            f.write(text)

        job["result_path"] = result_path
        job["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job["status"] = "completed"
        job["elapsed_sec"] = round(time.time() - t_start, 1)
        # モデルの用意（読み込み・初回ダウンロード）は音声の長さに比例しない固定費。
        # 実績の倍率はこれを除いた「実際に文字起こしにかかった時間」で測る。
        # 除かないと、初回のダウンロードが乗った1本目だけで倍率が何倍にも膨らむ。
        model_sec = float(job.get("model_load_sec") or 0)
        job["transcribe_sec"] = round(max(0.0, job["elapsed_sec"] - model_sec), 1)
        print(f"[KoeLog] 処理完了 {job['completed_at']} job={job_id} "
              f"所要={job['elapsed_sec']}秒（うちモデル準備={model_sec}秒）"
              f" 段階={job.get('quality_level')}")

        # 推定処理時間を実機に合わせていくため、実績を記録する（モックは除く）
        if not MOCK_MODE:
            record_quality_stat(job.get("quality_level"), job.get("duration_sec", 0),
                                job["transcribe_sec"])

        # メール通知
        send_notification(job.get("email", ""), job)

    except Exception as e:
        job["error"] = str(e)
        job["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job["status"] = "failed"
        print(f"[KoeLog] 処理失敗 job={job_id}: {e}")
    finally:
        # 前処理ファイルを削除
        if preprocessed_path and preprocessed_path != filepath and os.path.exists(preprocessed_path):
            try:
                os.remove(preprocessed_path)
            except OSError:
                pass


def queue_worker(worker_no: int):
    """FIFOキューからジョブを取り出して順番に処理するワーカー"""
    print(f"[KoeLog] ワーカー#{worker_no} 起動")
    while True:
        job_id = job_queue.get()
        try:
            process_job(job_id)
        except Exception as e:
            print(f"[KoeLog] ワーカー#{worker_no} 例外: {e}")
        finally:
            job_queue.task_done()


for _i in range(WORKER_COUNT):
    threading.Thread(target=queue_worker, args=(_i + 1,), daemon=True).start()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """メイン画面"""
    job_list = build_job_list()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "jobs": job_list,
        "max_file_size_mb": config.MAX_FILE_SIZE_MB,
        "allowed_extensions": ", ".join(sorted(config.ALLOWED_EXTENSIONS)),
        "quality_levels": presets.ui_levels(get_quality_stats()),
        "default_quality_level": get_default_quality_level(),
    })


@app.post("/upload")
async def upload(file: UploadFile = File(...), email: str = Form(""), output_format: str = Form("timestamp"), field: str = Form("general"), custom_prompt: str = Form(""), username: str = Form(""), quality_level: str = Form("")):
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

    # ジョブ作成（保存先は受付時点の設定で確定。以後の設定変更は次のジョブから）
    job_id = str(uuid.uuid4())
    safe_filename = f"{job_id}{ext}"
    upload_dir = get_upload_dir()
    result_dir = get_result_dir()
    # 段階も受付時点で確定する（未指定・不正値は管理画面の既定段階）
    level = (quality_level or "").strip()
    level = presets.normalize_level(level) if level else get_default_quality_level()
    try:
        os.makedirs(upload_dir, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"アップロード先フォルダを作成できません: {upload_dir} ({e.strerror or e})")
    filepath = os.path.abspath(os.path.join(upload_dir, safe_filename))

    try:
        with open(filepath, "wb") as f:
            f.write(contents)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"アップロード先に書き込めません: {upload_dir} ({e.strerror or e})")

    # 音声の長さを取得
    duration = get_audio_duration(filepath)

    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "seq": next_seq(),
            "filename": file.filename,
            "status": "waiting",
            "progress": 0,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "started_at": None,
            "completed_at": None,
            "result_path": None,
            "result_dir": result_dir,
            "error": None,
            "upload_path": filepath,
            "file_size_mb": round(size_mb, 1),
            "email": email.strip(),
            "output_format": output_format,
            "duration_sec": round(duration, 1),
            # 推定処理時間は段階ごとに変わる。このサーバーの実績があればそれを使う
            "estimated_sec": presets.estimate_sec(duration, level, get_quality_stats()),
            "quality_level": level,
            "field": field,
            "custom_prompt": custom_prompt.strip(),
            "username": username.strip() or "anonymous",
        }

    # FIFOキューの末尾に追加（ワーカーが受付順に処理）
    job_queue.put(job_id)
    position = len(waiting_job_ids_in_order())

    return JSONResponse(content={
        "job_id": job_id, "status": "ok", "queue_position": position,
        "quality_level": level,
        "quality_name": presets.get(level)["name"],
    })


@app.get("/status")
async def status():
    """ジョブ一覧JSON（result_path, upload_path はフロントに渡さない）"""
    return JSONResponse(content=build_job_list())


@app.post("/cancel/{job_id}")
async def cancel(job_id: str):
    """待機中ジョブの取消（キューから外す）。処理中・完了済みは対象外"""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="ジョブが見つかりません")
        if job["status"] != "waiting":
            raise HTTPException(status_code=400, detail="待機中のジョブのみ取消できます")
        job["status"] = "cancelled"
        del jobs[job_id]
    # キューには job_id が残るが、ワーカー側で jobs に無いものは読み飛ばす
    remove_job_files(job)
    return JSONResponse(content={"status": "ok", "job_id": job_id})


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
    return JSONResponse(content={"text": text, "filename": job["filename"], "job_id": job_id})


@app.get("/audio/{job_id}")
async def audio(job_id: str):
    """Stream audio file for playback"""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    upload_path = job.get("upload_path")
    if not upload_path or not os.path.exists(upload_path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(path=upload_path, media_type="audio/mpeg")


@app.post("/save/{job_id}")
async def save_result(job_id: str, request: Request):
    """Save edited result text"""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "completed" or not job["result_path"]:
        raise HTTPException(status_code=400, detail="Job not completed")
    body = await request.json()
    text = body.get("text", "")
    with open(job["result_path"], "w", encoding="utf-8-sig") as f:
        f.write(text)
    return JSONResponse(content={"status": "ok"})


@app.post("/delete/{job_id}")
async def delete(job_id: str):
    """ジョブ削除（待機中ならキューからも外れる）"""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="ジョブが見つかりません")
        if job["status"] == "processing":
            raise HTTPException(status_code=400, detail="処理中のジョブは削除できません")
        if job["status"] == "waiting":
            job["status"] = "cancelled"
        del jobs[job_id]

    remove_job_files(job)

    return RedirectResponse(url="/", status_code=303)


# --- 管理画面 ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """管理画面"""
    settings = load_settings()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "settings": settings,
        "default_upload_dir": os.path.abspath(config.UPLOAD_DIR),
        "default_result_dir": os.path.abspath(config.RESULT_DIR),
        "quality_levels": presets.ui_levels(get_quality_stats()),
        "quality_models": presets.model_status(get_quality_stats()),
        "default_quality_level": get_default_quality_level(),
    })


@app.post("/admin/quality/save")
async def admin_quality_save(default_quality_level: str = Form("")):
    """既定の段階を保存。次のジョブから有効（実行中・待機中のジョブは変えない）。

    /admin/save（SMTP）とは別のエンドポイントにしてある。
    /admin/save は「送られたフィールドだけ更新する」作りなので、そこには触らない。
    """
    raw = (default_quality_level or "").strip()
    if raw not in presets.PRESETS:
        return JSONResponse(content={
            "status": "error",
            "message": f"段階は {'/'.join(presets.LEVELS)} のいずれかを指定してください（受け取った値: {raw or '空'}）",
        })
    update_settings({"default_quality_level": raw})
    p = presets.get(raw)
    downloaded = presets.model_is_downloaded(p["model_size"])
    msg = f"既定の段階を「{raw}: {p['name']}」にしました（次のジョブから有効）"
    # 起動時はこのモデルを裏で読む。時間がかかる場合はここで伝える
    if not downloaded:
        warn = (f"このモデル（{p['model_size']}）はまだ取得していません。"
                f"最初のジョブ、または次回の起動時に約{presets.download_gb(raw)}GB の"
                f"ダウンロードが走り、そのぶん最初の1本が待たされます。")
    elif p["model_size"] == "large-v3":
        warn = (f"このモデル（{p['model_size']}）は大きいため、"
                f"次回の起動後に使えるようになるまで数分かかることがあります"
                f"（画面自体はすぐ開きます）。")
    else:
        warn = ""
    return JSONResponse(content={
        "status": "ok",
        "message": msg,
        "warning": warn,
        "default_quality_level": raw,
        "hour_minutes": presets.hour_minutes(raw, get_quality_stats()),
        "model_downloaded": downloaded,
    })


@app.get("/admin/quality/models")
async def admin_quality_models():
    """段階ごとのモデル取得状況（ネットワークには出ない。ローカルのキャッシュを見るだけ）"""
    return JSONResponse(content={
        "status": "ok",
        "models": presets.model_status(get_quality_stats()),
        "loaded": loaded_model_keys(),
        "loading": loading_model_keys(),
        "default_quality_level": get_default_quality_level(),
        "mock_mode": MOCK_MODE,
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
    """SMTP設定を保存（保存先など他の設定は保持）"""
    data = {
        "smtp_host": smtp_host.strip(),
        "smtp_port": smtp_port,
        "smtp_user": smtp_user.strip(),
        "smtp_password": smtp_password.strip(),
        "smtp_from": smtp_from.strip(),
        "smtp_use_tls": smtp_use_tls == "on",
        "mail_enabled": mail_enabled == "on",
    }
    update_settings(data)
    return JSONResponse(content={"status": "ok"})


@app.post("/admin/storage/save")
async def admin_storage_save(upload_dir: str = Form(""), result_dir: str = Form("")):
    """保存先設定を保存。空欄は既定（uploads/ results/）。次のジョブから有効"""
    upload_dir = upload_dir.strip()
    result_dir = result_dir.strip()
    errors = []
    ok, msg = validate_storage_path(upload_dir)
    if not ok:
        errors.append(f"アップロード先: {msg}")
    ok, msg = validate_storage_path(result_dir)
    if not ok:
        errors.append(f"結果保存先: {msg}")
    if errors:
        return JSONResponse(content={"status": "error", "message": " / ".join(errors)})
    update_settings({"upload_dir": upload_dir, "result_dir": result_dir})
    return JSONResponse(content={
        "status": "ok",
        "message": "保存先を保存しました（次のジョブから有効）",
        "effective_upload_dir": get_upload_dir(),
        "effective_result_dir": get_result_dir(),
    })


@app.post("/admin/storage/test")
async def admin_storage_test(upload_dir: str = Form(""), result_dir: str = Form(""), create: str = Form("off")):
    """保存先への書き込みテスト（一時ファイル作成→削除）。create=on なら無ければ作成"""
    do_create = create == "on"
    return JSONResponse(content={
        "status": "ok",
        "upload": test_storage_path(upload_dir, do_create),
        "result": test_storage_path(result_dir, do_create),
    })


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


@app.get("/admin/dictionary")
async def admin_dictionary():
    """Return dictionary entries as JSON"""
    entries = load_dictionary()
    return JSONResponse(content={"entries": entries})


@app.post("/admin/dictionary/save")
async def admin_dictionary_save(request: Request):
    """Save dictionary entries"""
    body = await request.json()
    entries = body.get("entries", [])
    # Validate: keep only entries with both from and to
    valid = [e for e in entries if e.get("from") and e.get("to")]
    save_dictionary(valid)
    return JSONResponse(content={"status": "ok", "count": len(valid)})


@app.get("/admin/dictionary/export")
async def admin_dictionary_export():
    """Export dictionary as CSV (UTF-8 BOM for Excel)"""
    entries = load_dictionary()
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM for Excel
    writer = csv.writer(buf)
    writer.writerow(["from", "to"])
    for e in entries:
        if e.get("from") and e.get("to"):
            writer.writerow([e["from"], e["to"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=koelog_dictionary.csv"},
    )


@app.post("/admin/dictionary/import")
async def admin_dictionary_import(file: UploadFile = File(...)):
    """Import dictionary from CSV (merge: skip duplicates)"""
    content = await file.read()
    # Handle BOM
    text = content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 2:
        return JSONResponse(content={"status": "error", "message": "Invalid CSV format"})

    imported = []
    for row in reader:
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            imported.append({"from": row[0].strip(), "to": row[1].strip()})

    existing = load_dictionary()
    existing_set = {(e["from"], e["to"]) for e in existing if e.get("from") and e.get("to")}

    added = 0
    for entry in imported:
        if (entry["from"], entry["to"]) not in existing_set:
            existing.append(entry)
            existing_set.add((entry["from"], entry["to"]))
            added += 1

    save_dictionary(existing)
    return JSONResponse(content={
        "status": "ok",
        "added": added,
        "skipped": len(imported) - added,
        "total": len(existing),
    })
