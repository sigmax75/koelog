"""KoeLog - 会議録音 文字起こしツール"""

import os
import io
import re
import csv
import gc
import time
import uuid
import wave
import queue
import shutil
import contextlib
import threading
import subprocess
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
# ディスクへの書き出し・ハッシュ計算など、待ちの長い処理をイベントループから
# 追い出すために使う（アップロード中に画面が止まらないようにするため）
from starlette.concurrency import run_in_threadpool
# 送信中に通信が切れたことを表す例外。捕まえないと ASGI の未処理例外になり、
# 実害が無いのにコンソールへ長い traceback が出る
from starlette.requests import ClientDisconnect

import json

import config
import presets
import audio_pipeline
import uploadstore
from audio_pipeline import MediaError
from uploadstore import UploadError

# --- 検証用モック（KOELOG_MOCK=1 のときだけ faster-whisper を使わない） ---
MOCK_MODE = os.environ.get("KOELOG_MOCK") == "1"
if not MOCK_MODE:
    from faster_whisper import WhisperModel

# KOELOG_SKIP_FFMPEG=1 で ffmpeg の処理（取り出し・再生用コピー）を丸ごと飛ばす。
#
# KOELOG_MOCK=1 は「faster-whisper を呼ばない」だけの意味にしてある。
# モックでも ffmpeg は動かす。そうしないと、今回の主目的である
# 「動画から音声をはがす」をモックで検証できない（以前は MOCK=1 で
# ffmpeg 前処理ごと飛ばしていた）。
# ffmpeg が入っていないPCで従来どおりの軽いモック検証をしたいときは、
# KOELOG_MOCK=1 と合わせて KOELOG_SKIP_FFMPEG=1 を指定する。
SKIP_FFMPEG = os.environ.get("KOELOG_SKIP_FFMPEG") == "1"

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


# --- ディスクの空き検査 ---
#
# サイズの上限は置かない方針なので、守りはここ1か所だけ。
# **アップロードを始める前に**断ることが大事。全部送り終わってから
# 「容量不足で失敗」が一番たちが悪い（送信時間が丸ごと無駄になる）。

def _gb(num_bytes: float) -> float:
    return round(num_bytes / (1024 ** 3), 2)


def _size_text(num_bytes: float) -> str:
    """人が読む大きさの表記。1GB 未満は MB で出す。

    すべて GB で出すと、短い音声のぶんが「0.0GB」になって何も伝わらない。
    """
    if num_bytes >= 1024 ** 3:
        return f"{_gb(num_bytes)}GB"
    if num_bytes >= 1024 ** 2:
        return f"{round(num_bytes / 1024 ** 2)}MB"
    return f"{max(1, round(num_bytes / 1024))}KB"


def estimate_required_bytes(size_bytes: int, duration_sec: float = 0.0) -> dict:
    """このファイルを処理しきるのに必要なディスク量の見積もり（内訳つき）

    内訳:
      * アップロードするファイルそのもの
      * 取り出した 16kHz/モノラルの WAV（約115MB/時間）
      * 再生用の軽い音声コピー（WAV の約1/8）
      * 安全余裕（ドライブをぎりぎりまで埋めない）

    音声の長さが分からない時点で呼ばれることがある（アップロード開始前は
    まだファイルがサーバーに無い）。そのときは WAV のぶんを
    config.WAV_UNKNOWN_FLOOR_GB（既定 1GB ＝ 約8.7時間ぶん）で見込む。
    """
    size_bytes = max(0, int(size_bytes or 0))
    if duration_sec and duration_sec > 0:
        wav = (duration_sec / 3600.0) * config.WAV_MB_PER_HOUR * 1024 ** 2
    else:
        wav = config.WAV_UNKNOWN_FLOOR_GB * 1024 ** 3
    playback = wav / 8.0
    reserve = config.DISK_RESERVE_GB * 1024 ** 3
    total = size_bytes + wav + playback + reserve
    return {
        "upload_bytes": size_bytes,
        "wav_bytes": wav,
        "playback_bytes": playback,
        "reserve_bytes": reserve,
        "total_bytes": total,
        "duration_known": bool(duration_sec and duration_sec > 0),
    }


def check_disk_space(target_dir: str, size_bytes: int, duration_sec: float = 0.0,
                     already_received: int = 0,
                     exclude_upload_id: str = "") -> dict:
    """保存先の空きが足りるか調べる。足りなければ理由つきのメッセージを返す。

    **進行中のアップロードが確保しているぶんを差し引いてから判定する。**
    `shutil.disk_usage().free` だけを見ていた時期は、2本の `/upload/init` が
    同時に来るとどちらも「空いている」と判断して両方受け付け、空きを
    二重取りしていた（実測: 空き13.3GB に 9.3GB × 2本が 200 で通った）。
    送信途中で実際に空きが尽きると `PUT /chunk` が 507 を返し、
    フロントはこれを再試行しないので、送信にかけた時間が丸ごと無駄になる。

    exclude_upload_id: 再開の検査では、その受付自身を予約から除く
      （already_received を引いた必要量と二重になるため）。

    戻り値: {"ok": bool, "message": str, ...内訳}
    """
    need = estimate_required_bytes(size_bytes, duration_sec)
    # 再開のときは、すでに届いているぶんはもう確保済みなので差し引く
    need["total_bytes"] -= max(0, int(already_received or 0))
    try:
        os.makedirs(target_dir, exist_ok=True)
        usage = shutil.disk_usage(target_dir)
        free = usage.free
    except OSError as e:
        return {
            "ok": False,
            "message": (f"保存先の空き容量を確認できませんでした: {target_dir} "
                        f"（{getattr(e, 'strerror', None) or e}）"),
            "free_bytes": 0, "reserved_bytes": 0, "usable_bytes": 0, **need,
        }
    # 進行中（受付済み・未完了）のアップロードが、これから書き込む予定の量。
    # 掃除で消える古い断片は数えない（数えると空きが永久に減る）。
    try:
        reserved = uploadstore.pending_reserved_bytes(
            target_dir, exclude_upload_id,
            float(getattr(config, "UPLOAD_PART_TTL_HOURS", 24)))
    except Exception as e:
        # ここで落として受付を止めない（従来どおり free だけで判定する）
        print(f"[KoeLog] 進行中アップロードの予約量を数えられませんでした: {e}")
        reserved = 0
    usable = max(0, free - reserved)
    extra = {"free_bytes": free, "reserved_bytes": reserved,
             "usable_bytes": usable}
    if usable >= need["total_bytes"]:
        return {"ok": True, "message": "", **extra, **need}

    wav_note = "約" if need["duration_known"] else "最低"
    reserved_note = ""
    if reserved > 0:
        reserved_note = (
            f"（うち {_size_text(reserved)} は進行中の別のアップロードが確保中。"
            f"いますぐ使えるのは {_size_text(usable)}）\n")
    message = (
        f"保存先（{target_dir}）の空き容量が足りないため、アップロードを始められません。\n"
        f"いまの空き: {_size_text(free)} / 必要な見込み: {_size_text(need['total_bytes'])}\n"
        f"{reserved_note}"
        f"内訳: このファイル {_size_text(need['upload_bytes'])} ＋ "
        f"変換後の音声 {wav_note}{_size_text(need['wav_bytes'])} ＋ "
        f"再生用コピー 約{_size_text(need['playback_bytes'])} ＋ "
        f"安全余裕 {_size_text(need['reserve_bytes'])}\n"
        f"空きを増やすか、管理画面の「保存先」を空きのあるドライブに変えてから、"
        f"もう一度お試しください。"
    )
    return {"ok": False, "message": message, **extra, **need}


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
    # 段階4(large-v3)を既定にすると、未取得なら約3.1GBのダウンロードぶん、
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
        # 音声の取り出し（動画からのはがし）にかかった時間。
        # 所要時間の実績（倍率）からはこれも差し引く。差し引かないと
        # 動画を通したときだけ倍率が膨らんで、推定処理時間が狂う。
        "extract_sec": job.get("extract_sec", 0),
        # 動画だった場合に混ぜた音声トラックの本数（1なら単一）
        "audio_streams": job.get("audio_streams", 0),
        # 処理後に元ファイル（数GBの動画など）を消したかどうか
        "original_removed": bool(job.get("original_removed")),
        # 失敗ではないが利用者に伝えるべきこと（ffmpeg が無い等）
        "warning": job.get("warning", ""),
    }


def build_job_list():
    """ジョブ一覧（新しい順、待機中には queue_position 付き）"""
    order = waiting_job_ids_in_order()
    position_map = {jid: i + 1 for i, jid in enumerate(order)}
    with jobs_lock:
        job_list = sorted(jobs.values(), key=lambda j: j["seq"], reverse=True)
        return [build_job_view(j, position_map) for j in job_list]


def remove_job_files(job: dict):
    """ジョブに紐づくファイルを削除

    playback_path（再生用の軽い音声コピー）も必ず含める。
    含めないと、元ファイルを消したあとにジョブを削除したとき、
    コピーだけが保存先に残り続ける。
    """
    for key in ("upload_path", "result_path", "playback_path"):
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


def prepare_media(job: dict) -> str:
    """元ファイル（音声でも動画でも）から文字起こし用の WAV を作り、そのパスを返す。

    旧 `preprocess_audio` の置き換え。変わった点は3つ。

      1. **動画でも同じ道を通る。** 音声トラックを ffmpeg ではがす。
         トラックが複数あれば全部混ぜる（既定の「チャンネル数が最多の1本」では
         参加者ごとにトラックが分かれた録画で一部の声が黙って落ちる）。
      2. **300秒のタイムアウトを撤廃した。** 何時間かかってもよい。
         そのかわり進捗が止まったときだけ失敗させる（audio_pipeline 側）。
      3. **黙って元ファイルで続行しない。** 失敗は MediaError として投げ、
         ジョブを「失敗」にして理由を画面に出す。

    例外: ffmpeg がそもそもインストールされていない場合だけは、従来どおり
    前処理なしで続行する（音声ファイルだけを扱っていた従来の運用を壊さない
    ため）。ただし黙ってではなく job["warning"] に残して画面に出す。
    """
    input_path = job["upload_path"]
    output_path = input_path + ".preprocessed.wav"

    if not audio_pipeline.ffmpeg_available():
        # ここで失敗にすると、ffmpeg 無しで音声ファイルだけ使っていた
        # 従来の運用が全部止まる。続行はするが、黙ってはいない。
        job["warning"] = ("ffmpeg が見つからないため、音声の前処理（16kHz化・"
                          "音量の正規化）を省きました。動画ファイルはこの状態では"
                          "処理できません。サーバーに ffmpeg を用意してください。")
        print(f"[KoeLog] ffmpeg が見つかりません（前処理をスキップ）: "
              f"FFMPEG_PATH={config.FFMPEG_PATH}")
        return input_path

    job["phase"] = "extract"
    t0 = time.time()
    info = audio_pipeline.probe_media(input_path)
    job["audio_streams"] = info.audio_streams
    if info.duration_sec > 0:
        job["duration_sec"] = round(info.duration_sec, 1)
        job["estimated_sec"] = presets.estimate_sec(
            info.duration_sec, job.get("quality_level"), get_quality_stats())
    if info.audio_streams > 1:
        print(f"[KoeLog] 音声トラックが {info.audio_streams} 本あります。"
              f"全部混ぜて文字起こしします job={job['job_id']}")

    # 第2段階ではここに progress_cb を渡して「取り出し ○%」を画面に出す。
    # いまは取り出しの進捗は画面に出さない（第1段階の範囲外）。
    audio_pipeline.extract_audio(input_path, output_path, info)
    job["extract_sec"] = round(time.time() - t0, 1)
    print(f"[KoeLog] 音声の取り出し完了 job={job['job_id']} "
          f"{job['extract_sec']}秒 トラック数={info.audio_streams} "
          f"video={info.video_streams}")
    return output_path


def make_playback_copy_for(job: dict, wav_path: str):
    """再生用の軽い音声コピーを作る。作れたら job に記録する。

    元ファイル（数GBの動画）を消す前に必ず呼ぶ。
    **コピーを作れなかったときは元ファイルを消さない**（消すと既存の
    「音声再生＋タイムスタンプ連動シーク」が動かなくなる）。
    """
    if wav_path == job["upload_path"]:
        # 前処理が走っていない（ffmpeg が無い）。コピーも作れない
        return None
    out = os.path.join(os.path.dirname(job["upload_path"]),
                       f"{job['job_id']}.play.mp3")
    try:
        audio_pipeline.make_playback_copy(wav_path, out)
    except Exception as e:
        print(f"[KoeLog] 再生用コピーを作れませんでした（元ファイルは残します）"
              f" job={job['job_id']}: {e}")
        return None
    job["playback_path"] = out
    return out


# ffmpeg が標準エラーに出す長さの表記を読むための正規表現。
#   ヘッダ由来:  Duration: 00:02:10.00, start: 0.000000, bitrate: ...
# デコード中の `time=` を読む経路（丸ごとデコードする2段目）は
# 2026-11-26 に撤去したので、その正規表現も置いていない
# （長時間ファイルで本処理とディスク I/O を取り合うため。_duration_from_ffmpeg 参照）。
_FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def _hms_to_sec(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


def _duration_from_wave(filepath: str) -> float:
    """WAV の長さを Python 標準ライブラリで読む（外部プロセスを起動しない）

    ヘッダの長さは常に信用できるわけではない。
      * ffmpeg 等がパイプに書き出した WAV は、書き始めの時点で長さが分からないので
        data チャンクのサイズに 0xFFFFFFFF 等の「未定」を書く。wave はこれを
        そのままフレーム数（2147483647）として返すため、実体 6.5 秒の
        208KB のファイルが約37時間になる。
      * 録音中にプロセスが落ちて末尾のサイズを書き戻せなかった WAV も同じ。
    ヘッダの言う音声データ量がファイルの実サイズを超えていたら、その長さは
    成立しない。そういうときは 0.0 を返して、呼び出し側の ffmpeg 経路に回す
    （ffmpeg はデコードして実際の長さを出せる）。
    """
    with contextlib.closing(wave.open(filepath, "rb")) as w:
        rate = w.getframerate()
        frames = w.getnframes()
        block = w.getnchannels() * w.getsampwidth()   # 1フレームのバイト数
    if rate <= 0 or frames <= 0:
        return 0.0
    if block > 0 and frames * block > os.path.getsize(filepath):
        # ヘッダを信用できない（長さ未定・途中で切れた）→ ffmpeg に任せる
        print(f"[KoeLog] WAVヘッダの長さがファイルサイズと合いません"
              f"（frames={frames} / ffmpegで測り直します）: {filepath}")
        return 0.0
    return frames / float(rate)


def _run_ffmpeg_stderr(args: list, timeout: int) -> str:
    """ffmpeg を起動して標準エラーを返す。

    長さを聞くだけの呼び出しでは ffmpeg は「出力ファイルが無い」として
    終了コード 1 を返すのが正常なので、終了コードは見ない。
    """
    result = subprocess.run(
        [config.FFMPEG_PATH, "-hide_banner", "-nostdin"] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    return result.stderr or ""


def _duration_from_ffmpeg(filepath: str) -> float:
    """ffmpeg（ffprobe ではない）から長さを取る。

    ffprobe を使わないのは、サーバーのアプリケーション制御ポリシーが
    ffprobe.exe だけをブロックしていて（WinError 4551）、長さが 0 になり
    推定処理時間の表示と実績学習が働かなくなっていたため。
    ポリシーは実行ファイル単位なので、前処理で使えている ffmpeg は通る。

    **ヘッダの Duration だけを読む。デコードはしない。**
    以前はヘッダに長さが無いとき（`Duration: N/A`）の2段目として
    `-f null -` でファイルを丸ごとデコードし、最後の `time=` を長さとして
    採っていたが、撤去した。理由:
      * ここは受付直後に別スレッドで走る。長時間ファイル（926MB の動画など）だと
        丸ごとデコードする ffmpeg が、本処理の取り出し用 ffmpeg と並走して
        ディスク I/O を取り合う。**本処理を遅くする**
      * 得られるのは推定処理時間の表示だけ（便利機能）。失敗しても 0.0 を
        返して文字起こしは通る。本処理と I/O を取り合う価値がない
      * 残っていた `timeout=300` は、長時間ファイルでは「長さは分からないまま
        5分 I/O を食って終わる」という最悪の形になっていた
    長さが分からないときは 0.0 を返す。画面は `duration_sec > 0` /
    `estimated_sec > 0` のときだけ表示するので、欄が空のまま壊れない。
    正確な長さは、ワーカーの取り出し処理（prepare_media）が後から入れ直す。
    """
    try:
        stderr = _run_ffmpeg_stderr(["-i", filepath], 30)
        found = _FFMPEG_DURATION_RE.findall(stderr)
        if found:
            sec = _hms_to_sec(*found[0])
            if sec > 0:
                return sec
    except Exception as e:
        print(f"[KoeLog] 音声の長さの取得（ヘッダ）に失敗: {e}")

    print(f"[KoeLog] ヘッダに長さがありませんでした（推定処理時間は表示しません）: "
          f"{os.path.basename(filepath)}")
    return 0.0


def get_audio_duration(filepath: str) -> float:
    """音声の長さ（秒）を取得する。取得できなければ 0.0（従来どおり続行する）"""
    if os.path.splitext(filepath)[1].lower() == ".wav":
        try:
            sec = _duration_from_wave(filepath)
            if sec > 0:
                return sec
        except Exception as e:
            # 拡張子が .wav でも wave で読めない場合（圧縮WAV等）は ffmpeg に回す
            print(f"[KoeLog] WAVヘッダを読めませんでした（ffmpegで再試行）: {e}")
    try:
        return _duration_from_ffmpeg(filepath)
    except Exception as e:
        print(f"[KoeLog] 音声の長さを取得できませんでした: {e}")
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
        # 音声の取り出し（動画ならここで音声をはがす）。
        #
        # KOELOG_MOCK=1 は「faster-whisper を呼ばない」だけの意味にしてある。
        # ffmpeg は動かす（そうしないと動画からの取り出しをモックで検証できない）。
        # ffmpeg の無いPCで従来どおりモック検証したいときは
        # KOELOG_SKIP_FFMPEG=1 を足す。
        if SKIP_FFMPEG:
            preprocessed_path = filepath
        else:
            preprocessed_path = prepare_media(job)

        # 再生用の軽い音声コピーを、**元ファイルを消す前に**作っておく。
        # これが作れていないときは後段で元ファイルを消さない。
        if not SKIP_FFMPEG:
            make_playback_copy_for(job, preprocessed_path)

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
        # 音声の取り出し（動画からのはがし）も、モデルの用意と同じく
        # 「文字起こしそのものではない時間」なので倍率から外す。
        # 外さないと、動画を通したときだけ実績の倍率が膨らむ。
        model_sec = float(job.get("model_load_sec") or 0)
        extract_sec = float(job.get("extract_sec") or 0)
        job["transcribe_sec"] = round(
            max(0.0, job["elapsed_sec"] - model_sec - extract_sec), 1)
        print(f"[KoeLog] 処理完了 {job['completed_at']} job={job_id} "
              f"所要={job['elapsed_sec']}秒（うちモデル準備={model_sec}秒 "
              f"音声の取り出し={extract_sec}秒）"
              f" 段階={job.get('quality_level')}")

        # 元ファイル（数GBの動画など）を消す。再生用の軽いコピーだけ残す。
        # 残すとディスクが動画のぶんだけ増え続け、いずれ「空きが無い」が
        # 手作業の原因になる（オーナー判断1）。
        remove_original_file(job)

        # 推定処理時間を実機に合わせていくため、実績を記録する（モックは除く）
        if not MOCK_MODE:
            record_quality_stat(job.get("quality_level"), job.get("duration_sec", 0),
                                job["transcribe_sec"])

        # メール通知
        send_notification(job.get("email", ""), job)

    except MediaError as e:
        # 音声の取り出しに関する失敗。message は利用者向けの日本語なので
        # そのまま画面に出す（黙って続行はしない）。
        # 失敗したジョブの元ファイルは再試行できるように残す。
        job["error"] = e.message
        job["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job["status"] = "failed"
        print(f"[KoeLog] 音声の取り出しに失敗 job={job_id}: {e.message} "
              f"/ detail: {e.detail}")
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


def remove_original_file(job: dict):
    """処理が終わった元ファイル（数GBの動画など）を消す。

    消すのは次の条件がそろったときだけ。
      * 再生用の軽い音声コピーが実在する
        （無いまま消すとプレビューの音声再生とタイムスタンプ連動シークが壊れる）
      * 元ファイルとコピーが別物である

    失敗したジョブでは呼ばない（再試行できるように元ファイルを残す）。
    """
    play = job.get("playback_path")
    orig = job.get("upload_path")
    if not orig or not os.path.exists(orig):
        return
    if not play or not os.path.exists(play) or os.path.abspath(play) == os.path.abspath(orig):
        print(f"[KoeLog] 再生用コピーが無いため元ファイルは残します job={job['job_id']}")
        return
    try:
        size = os.path.getsize(orig)
        os.remove(orig)
        job["original_removed"] = True
        print(f"[KoeLog] 元ファイルを削除しました job={job['job_id']} "
              f"({_gb(size)}GB) 再生用コピーを残しています: "
              f"{os.path.basename(play)} ({_gb(os.path.getsize(play))}GB)")
    except OSError as e:
        print(f"[KoeLog] 元ファイルを削除できませんでした job={job['job_id']}: {e}")


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


def _cleanup_parts_once():
    """中断されたまま放置されたアップロードの断片を掃除する。

    放っておくと数GBの断片がディスクに残り続け、「空きが無い」という形で
    結局また手作業（容量の確保）の原因になる。
    """
    try:
        ttl = float(getattr(config, "UPLOAD_PART_TTL_HOURS", 24))
        n = uploadstore.cleanup_stale(get_upload_dir(), ttl)
        if n:
            print(f"[KoeLog] 中断されたアップロードの断片を {n} 件片付けました"
                  f"（{ttl}時間以上更新なし）")
    except Exception as e:
        print(f"[KoeLog] アップロード断片の掃除に失敗しました: {e}")


def _cleanup_parts_daemon():
    """起動時に1回、そのあと1日1回掃除する"""
    _cleanup_parts_once()
    while True:
        time.sleep(24 * 3600)
        _cleanup_parts_once()


threading.Thread(target=_cleanup_parts_daemon, daemon=True,
                 name="koelog-parts-cleanup").start()

# --- 第2段階で足すもの（ここに置く） ---
#
#  * ジョブの永続化・再開: `jobstore.py` を新設し、起動時に
#    「waiting はキューへ、processing は waiting に戻す」復元をこの位置で行う。
#    いま jobs は dict（メモリのみ）なので、再起動で履歴が消える。
#    uploadstore._atomic_write_json と同じ「一時ファイル → os.replace」で書く。
#  * 孤児ファイルの掃除: プロセスが強制終了されると
#    `<uuid>.<ext>.preprocessed.wav` が残る（finally は走らない）。
#    上の _cleanup_parts_once と同じ場所に、jobs に無い preprocessed.wav を
#    消す処理を足す。
#  * チャンク分割の文字起こしと進捗: process_job の中で
#    audio_pipeline.find_split_points() → チャンクごとに model.transcribe()
#    → `work/<job_id>/chunk_NNNN.json` にチェックポイント。
#    進捗（処理済み秒／全体秒）は job["progress_sec"] に書き、
#    build_job_view で画面へ渡す。


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """メイン画面"""
    job_list = build_job_list()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "jobs": job_list,
        # サイズ上限・拡張子の白リストは撤廃したのでテンプレートにも渡さない
        "upload_chunk_mb": config.UPLOAD_CHUNK_MB,
        "quality_levels": presets.ui_levels(get_quality_stats()),
        "default_quality_level": get_default_quality_level(),
    })


# --- アップロード共通 ---

_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def safe_ext(filename: str) -> str:
    """保存名に使う拡張子を取り出す。

    **対応形式の判定には使わない。** 拡張子の白リストは撤廃した
    （絞ると、その都度利用者が手で変換することになる）。読めるかどうかは
    ffmpeg 自身に判定させる（audio_pipeline.probe_media）。
    ここで形を確かめているのは、保存するファイル名に変な文字を混ぜないためだけ。
    """
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if _EXT_RE.match(ext) else ""


def _job_settings_from(data: dict) -> dict:
    """ブラウザから届いた設定を、ジョブ用に正規化する。

    quality_level（画面で選んだ段階）が届かないと、画面上は選べているのに
    管理画面の既定段階で処理されてしまう。過去に実際に起きた不具合なので、
    ここを通る値は必ず1か所で確定させる。
    """
    level = str(data.get("quality_level") or "").strip()
    level = presets.normalize_level(level) if level else get_default_quality_level()
    return {
        "email": str(data.get("email") or "").strip(),
        "output_format": str(data.get("output_format") or "timestamp"),
        "field": str(data.get("field") or "general"),
        "custom_prompt": str(data.get("custom_prompt") or "").strip(),
        "username": str(data.get("username") or "").strip() or "anonymous",
        "quality_level": level,
    }


def register_job(filename: str, filepath: str, size_bytes: int,
                 settings: dict, duration_hint: float = 0.0) -> dict:
    """保存済みのファイルからジョブを作ってキューに入れる。

    **音声の長さはここでは測らない。** 旧実装は `async def upload` の中から
    同期の `get_audio_duration()` を呼んでいて、長い・遅い形式のときに
    イベントループを占有し、サーバー全体（他の人の画面も）が止まっていた。
    いまは
      * ブラウザが測れた長さ（duration_hint）を暫定値として使い、
      * 正確な値は別スレッドの先読み（_probe_duration_async）と
        ワーカーの取り出し処理（prepare_media）で後から入れ直す。
    """
    job_id = os.path.splitext(os.path.basename(filepath))[0]
    level = settings["quality_level"]
    duration = max(0.0, float(duration_hint or 0.0))
    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "seq": next_seq(),
            "filename": filename,
            "status": "waiting",
            "progress": 0,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "started_at": None,
            "completed_at": None,
            "result_path": None,
            "result_dir": get_result_dir(),
            "error": None,
            "upload_path": filepath,
            # 再生用の軽い音声コピー（処理後に元ファイルを消したあとはこちらを再生する）
            "playback_path": None,
            "original_removed": False,
            "warning": "",
            "file_size_mb": round(size_bytes / (1024 * 1024), 1),
            "email": settings["email"],
            "output_format": settings["output_format"],
            "duration_sec": round(duration, 1),
            # 推定処理時間は段階ごとに変わる。このサーバーの実績があればそれを使う
            "estimated_sec": presets.estimate_sec(duration, level, get_quality_stats()),
            "quality_level": level,
            "field": settings["field"],
            "custom_prompt": settings["custom_prompt"],
            "username": settings["username"],
            "audio_streams": 0,
            "extract_sec": 0,
        }
        job = jobs[job_id]

    # 長さの先読みは別スレッドで（イベントループを止めない）
    threading.Thread(target=_probe_duration_async, args=(job_id,), daemon=True,
                     name=f"koelog-probe-{job_id[:8]}").start()

    # FIFOキューの末尾に追加（ワーカーが受付順に処理）
    job_queue.put(job_id)
    return job


def _probe_duration_async(job_id: str):
    """受付後に音声の長さを測って、待機中の推定時間の表示に反映する。

    ワーカーの取り出し処理でも測り直すので、ここが失敗しても処理は進む。
    イベントループとは別のスレッドなので、遅い形式でも画面は止まらない。
    """
    with jobs_lock:
        job = jobs.get(job_id)
        path = job.get("upload_path") if job else None
    if not path or not os.path.exists(path):
        return
    try:
        duration = get_audio_duration(path)
    except Exception as e:
        print(f"[KoeLog] 長さの先読みに失敗しました job={job_id}: {e}")
        return
    if duration <= 0:
        return
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") not in ("waiting", "processing"):
            return
        job["duration_sec"] = round(duration, 1)
        job["estimated_sec"] = presets.estimate_sec(
            duration, job.get("quality_level"), get_quality_stats())


# --- 分割アップロード（上限なし・途中から再開できる） ---
#
# 流れ:
#   1. POST /upload/init          ファイル名とサイズを申告。**ここで空き容量を検査**
#   2. PUT  /upload/{id}/chunk    16MB ずつ生のバイナリで送る（multipart を使わない）
#   3. POST /upload/{id}/finish   サイズを照合して本来の置き場所へ移し、ジョブを作る
#   補助: GET /upload/{id}（どこまで届いたか）/ POST /upload/{id}/abort（破棄）
#
# multipart を使わない理由: starlette の multipart は OS の一時フォルダ
# （通常 C:）に SpooledTemporaryFile を作るので、ファイルが
# 「一時フォルダ → メモリ → 保存先」と最大3回扱われる。保存先ドライブだけで
# 完結させたいので、生のボディをストリームで受けて直接 .part に追記する。

# 「空きの判定 → 受付の作成（予約の成立）」を直列にするためのロック。
# 握っているあいだは await しないこと（await すると他のリクエストが
# この区間に入り込み、二重取りの穴が戻る）。
_upload_init_lock = threading.Lock()


@app.post("/upload/init")
async def upload_init(request: Request):
    """アップロードの受付。**送り始める前に**空き容量を検査して、足りなければ断る。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="受付情報（JSON）を読めませんでした")

    filename = str(body.get("filename") or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="ファイル名が空です")
    try:
        size = int(body.get("size") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="ファイルサイズの指定が正しくありません")
    if size <= 0:
        raise HTTPException(status_code=400, detail="ファイルサイズが 0 です（中身が空のファイルは処理できません）")
    resume_key = str(body.get("resume_key") or "")
    try:
        duration_hint = float(body.get("duration_sec") or 0)
    except (TypeError, ValueError):
        duration_hint = 0.0

    upload_dir = get_upload_dir()
    try:
        os.makedirs(upload_dir, exist_ok=True)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"アップロード先フォルダを作成できません: {upload_dir} "
                   f"({getattr(e, 'strerror', None) or e})")

    # 「空きの判定」と「受付の作成（＝予約の成立）」は**ひとまとまりで行う**。
    #
    # 分けていると、判定と作成のあいだに別の init が割り込み、両方が
    # 「空いている」と判断して空きを二重取りする（実測で再現した不具合）。
    # 作成が終わって初めて `.part` と meta ができ、
    # uploadstore.pending_reserved_bytes から見えるようになるため、
    # ここを直列にしないと予約そのものが意味を持たない。
    #
    # この区間では await しない（＝イベントループを手放さない）ので、
    # 同じループの別リクエストにも、別スレッドにも割り込まれない。
    # 中身はディスクの空き取得・断片フォルダの一覧・小さなファイル作成だけで、
    # 大きな入出力は含まない。
    with _upload_init_lock:
        # 途中まで届いている同じファイルがあれば、その続きから
        try:
            existing = uploadstore.find_by_resume_key(upload_dir, resume_key)
        except Exception:
            existing = None
        received = 0
        resumed = False
        if existing and int(existing.get("size", 0)) == size:
            upload_id = existing["upload_id"]
            received = uploadstore.received_bytes(upload_dir, upload_id)
            resumed = True
        else:
            upload_id = None

        # **空き容量の検査はここ。**送り終わってから失敗させない。
        # 再開のときは自分自身を予約から除く（already_received と二重になる）。
        disk = check_disk_space(upload_dir, size, duration_hint,
                                already_received=received,
                                exclude_upload_id=upload_id or "")
        if not disk["ok"]:
            return JSONResponse(status_code=507, content={
                "status": "error",
                "detail": disk["message"],
                "free_gb": _gb(disk["free_bytes"]),
                "reserved_gb": _gb(disk.get("reserved_bytes", 0)),
                "usable_gb": _gb(disk.get("usable_bytes", disk["free_bytes"])),
                "required_gb": _gb(disk["total_bytes"]),
            })

        if upload_id is None:
            meta = uploadstore.create(upload_dir, filename, size, resume_key,
                                      safe_ext(filename))
            upload_id = meta["upload_id"]
        elif existing.get(uploadstore.RELEASED_KEY):
            # 前回の送信が失敗して予約を手放していた受付を、続きから再開する。
            # **空きの検査に通ったあとに**印を下ろす（通らなければ手放したまま）。
            uploadstore.reactivate(upload_dir, existing)

    return JSONResponse(content={
        "status": "ok",
        "upload_id": upload_id,
        "received": received,
        "size": size,
        "resumed": resumed,
        "chunk_size": config.UPLOAD_CHUNK_MB * 1024 * 1024,
        "free_gb": _gb(disk["free_bytes"]),
        "reserved_gb": _gb(disk.get("reserved_bytes", 0)),
        "usable_gb": _gb(disk.get("usable_bytes", disk["free_bytes"])),
        "required_gb": _gb(disk["total_bytes"]),
    })


@app.get("/upload/{upload_id}")
async def upload_state(upload_id: str):
    """どこまで届いているか（ブラウザが再開位置を知るため）"""
    upload_dir = get_upload_dir()
    try:
        meta = uploadstore.load_meta(upload_dir, upload_id)
    except UploadError as e:
        return JSONResponse(status_code=e.status_code,
                            content={"status": "error", "detail": e.message})
    return JSONResponse(content={
        "status": "ok",
        "upload_id": upload_id,
        "received": uploadstore.received_bytes(upload_dir, upload_id),
        "size": int(meta.get("size", 0)),
        "filename": meta.get("filename", ""),
    })


@app.put("/upload/{upload_id}/chunk")
async def upload_chunk(upload_id: str, request: Request, offset: int = 0):
    """断片を1つ受け取って追記する。

    **ファイル全体をメモリに載せない。** ここで扱うのは1断片だけなので、
    ファイルが何GBでもサーバーのメモリ使用量は変わらない。
    申告より大きい断片は途中で打ち切る（メモリを守る）。

    **メモリに載る上限は、断片の既定（config.UPLOAD_CHUNK_MB = 16MB）の
    2倍の 32MB。** 16MB ちょうどで打ち切らないのは、ブラウザが送る1断片の
    実バイト数が設定値とぴったり一致しない場合（最後の断片・将来
    UPLOAD_CHUNK_MB を変えたときの古いタブ・API を直接叩く利用者）に、
    正しい送信を 413 で弾いてしまわないため。2倍までは受け取り、
    それを超えたら打ち切る。
    """
    upload_dir = get_upload_dir()
    # 既定 16MB の2倍 = 32MB まで受け取る（理由は上の docstring）
    limit = config.UPLOAD_CHUNK_MB * 1024 * 1024 * 2
    buf = bytearray()
    try:
        async for part in request.stream():
            buf.extend(part)
            if len(buf) > limit:
                return JSONResponse(status_code=413, content={
                    "status": "error",
                    "detail": (f"1回に送れる大きさの上限"
                               f"（{config.UPLOAD_CHUNK_MB * 2}MB）を超えています"
                               f"（通常は {config.UPLOAD_CHUNK_MB}MB ずつ送ります）"),
                })
    except ClientDisconnect:
        # 送信中に通信が切れた（ブラウザを閉じた・回線が落ちた）。
        # **途中まで届いた分は追記しない**（追記するとファイルが壊れる）。
        # ブラウザは次に `GET /upload/{id}` で現在位置を聞いて続きから送り直す。
        # ここで捕まえないと ClientDisconnect が ASGI の未処理例外になり、
        # 画面に何の問題も無いのにコンソールへ長い traceback が出る。
        print(f"[KoeLog] 送信が途中で切れました（追記していません）"
              f" upload_id={upload_id} offset={offset} "
              f"受信途中={len(buf)}バイト / 続きから再開できます")
        return JSONResponse(status_code=499, content={
            "status": "error",
            "detail": "送信が途中で切れました。続きから送り直してください。",
            "received": uploadstore.received_bytes(upload_dir, upload_id),
        })
    data = bytes(buf)
    del buf
    if not data:
        return JSONResponse(status_code=400,
                            content={"status": "error", "detail": "中身が空の断片です"})

    try:
        # 1断片ごとに壊れていないか確かめる（sha256 が使えない環境では crc32）
        verified = uploadstore.verify_chunk(
            data,
            request.headers.get("x-chunk-sha256", ""),
            request.headers.get("x-chunk-crc32", ""),
        )
        received = await run_in_threadpool(
            uploadstore.append_chunk, upload_dir, upload_id, int(offset), data)
    except UploadError as e:
        content = {"status": "error", "detail": e.message}
        content.update(e.extra)
        return JSONResponse(status_code=e.status_code, content=content)
    except OSError as e:
        return JSONResponse(status_code=507, content={
            "status": "error",
            "detail": (f"アップロード先に書き込めませんでした: "
                       f"{getattr(e, 'strerror', None) or e}"
                       f"（ディスクの空きが尽きた可能性があります）"),
        })
    return JSONResponse(content={"status": "ok", "received": received,
                                 "verified": verified})


@app.post("/upload/{upload_id}/finish")
async def upload_finish(upload_id: str, request: Request):
    """受信完了 → サイズとハッシュを確認 → ジョブ作成・キュー投入"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    upload_dir = get_upload_dir()
    try:
        meta = uploadstore.load_meta(upload_dir, upload_id)
        ext = meta.get("ext") or safe_ext(meta.get("filename", ""))
        job_id = str(uuid.uuid4())
        dest = os.path.abspath(os.path.join(upload_dir, f"{job_id}{ext}"))
        # サイズ照合と全体ハッシュの計算はディスクを読むので別スレッドで
        done = await run_in_threadpool(uploadstore.finish, upload_dir, upload_id, dest)
    except UploadError as e:
        content = {"status": "error", "detail": e.message}
        content.update(e.extra)
        return JSONResponse(status_code=e.status_code, content=content)
    except OSError as e:
        return JSONResponse(status_code=500, content={
            "status": "error",
            "detail": f"ファイルを保存先に移せませんでした: "
                      f"{getattr(e, 'strerror', None) or e}",
        })

    settings = _job_settings_from(body)
    try:
        duration_hint = float(body.get("duration_sec") or 0)
    except (TypeError, ValueError):
        duration_hint = 0.0
    job = register_job(meta.get("filename", f"{job_id}{ext}"), dest,
                       done.get("final_size", 0), settings, duration_hint)
    position = len(waiting_job_ids_in_order())
    print(f"[KoeLog] 受付完了 job={job['job_id']} file={job['filename']} "
          f"{job['file_size_mb']}MB sha256={done.get('sha256')} "
          f"段階={settings['quality_level']}")
    return JSONResponse(content={
        "job_id": job["job_id"], "status": "ok", "queue_position": position,
        "quality_level": settings["quality_level"],
        "quality_name": presets.get(settings["quality_level"])["name"],
        # 繋いだファイルが壊れていないことの確認に使える（検証・ログ用）
        "sha256": done.get("sha256", ""),
        "size": done.get("final_size", 0),
    })


@app.post("/upload/{upload_id}/abort")
async def upload_abort(upload_id: str, keep: int = 0):
    """アップロードを中断する。

    keep=0（既定）: 断片ごと破棄する。利用者が「外す」を押したとき
      （このファイルはもう上げない、という意思表示）に使う。
    keep=1: **ディスクの空きの予約だけを手放し、断片は残す。**
      送信が失敗したときに画面から呼ぶ。こうすると
        * 失敗した送信が他のアップロードを最長24時間ブロックしなくなる
        * それでも同じファイルを上げ直せば**続きから**再開できる
      の両方が成り立つ。残った断片は、再開されなければ
      `UPLOAD_PART_TTL_HOURS` の掃除で消える。
    """
    upload_dir = get_upload_dir()
    if keep:
        released = uploadstore.release(upload_dir, upload_id)
        return JSONResponse(content={"status": "ok", "released": bool(released),
                                     "kept": bool(released)})
    uploadstore.delete(upload_dir, upload_id)
    return JSONResponse(content={"status": "ok", "released": True, "kept": False})


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), email: str = Form(""), output_format: str = Form("timestamp"), field: str = Form("general"), custom_prompt: str = Form(""), username: str = Form(""), quality_level: str = Form("")):
    """音声・動画ファイルの一括アップロード → ジョブ作成（従来方式・互換用）

    画面は分割アップロード（/upload/init → chunk → finish）を使う。
    こちらは API を直接叩いている自動化のために残してある。

    変わった点:
      * サイズ上限と拡張子の白リストを撤廃した
      * `await file.read()` でファイル全体をメモリに載せるのをやめ、
        1MB ずつ読んでディスクへ書き出す形にした
      * 音声の長さの取得をここでは行わない（イベントループを止めないため）
    """
    ext = safe_ext(file.filename or "")
    upload_dir = get_upload_dir()
    try:
        os.makedirs(upload_dir, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"アップロード先フォルダを作成できません: {upload_dir} ({e.strerror or e})")

    # 送られてくる大きさが分かるなら、書き始める前に空きを確かめる。
    #
    # Content-Length は multipart 全体の大きさなので、ファイル本体より
    # 少し大きい（境界文字列と他のフォーム項目を含む）。見積もりとしては
    # 安全側に出るので、そのまま必要量の計算に使う。
    # 取れないとき（chunked 等）は何もしない。その場合でも書き込みに失敗すれば
    # 下の except OSError が 507 を返すので、安全側には倒れる。
    #
    # ここで断れると、保存先に書き始めてから失敗するのを防げる
    # （分割アップロードの /upload/init と同じ考え方）。
    #
    # **限界を書いておく:** multipart の解析は、この関数に入る前に
    # フレームワーク側で終わっている（UploadFile はそのとき作られる）。
    # つまりここで断れるのは「保存先への書き込み」より前であって、
    # 「本体の受信」より前ではない。受信そのものを始める前に断りたいなら、
    # 画面と同じ分割アップロード（/upload/init）を使うこと。
    try:
        declared = int(request.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > 0:
        disk = check_disk_space(upload_dir, declared)
        if not disk["ok"]:
            raise HTTPException(status_code=507, detail=disk["message"])

    job_id = str(uuid.uuid4())
    filepath = os.path.abspath(os.path.join(upload_dir, f"{job_id}{ext}"))

    size = 0
    try:
        with open(filepath, "wb") as f:
            while True:
                buf = await file.read(1024 * 1024)
                if not buf:
                    break
                size += len(buf)
                f.write(buf)
    except OSError as e:
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise HTTPException(status_code=507, detail=f"アップロード先に書き込めません: {upload_dir} ({getattr(e, 'strerror', None) or e})")

    if size == 0:
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="中身が空のファイルです")

    settings = _job_settings_from({
        "email": email, "output_format": output_format, "field": field,
        "custom_prompt": custom_prompt, "username": username,
        "quality_level": quality_level,
    })
    job = register_job(file.filename, filepath, size, settings)
    position = len(waiting_job_ids_in_order())

    return JSONResponse(content={
        "job_id": job["job_id"], "status": "ok", "queue_position": position,
        "quality_level": settings["quality_level"],
        "quality_name": presets.get(settings["quality_level"])["name"],
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


#  拡張子 → media_type（元ファイルを返す場合の宣言用）
_AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".mp4": "audio/mp4", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".flac": "audio/flac", ".webm": "audio/webm",
}


@app.get("/audio/{job_id}")
async def audio(job_id: str):
    """プレビューの音声再生用。

    処理が終わったジョブでは元ファイル（数GBの動画）を消しているので、
    **再生用の軽い音声コピー（mp3）を返す。**
    コピーが無い場合（処理前・失敗・ffmpeg 無し）は元ファイルを返す。
    タイムスタンプ連動シークは時間軸が同じなのでそのまま動く
    （コピーは取り出した音声そのままで、頭を落としたりしていない）。
    """
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    play_path = job.get("playback_path")
    if play_path and os.path.exists(play_path):
        return FileResponse(path=play_path, media_type="audio/mpeg")
    upload_path = job.get("upload_path")
    if not upload_path or not os.path.exists(upload_path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    ext = os.path.splitext(upload_path)[1].lower()
    return FileResponse(path=upload_path,
                        media_type=_AUDIO_MEDIA_TYPES.get(ext, "audio/mpeg"))


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
