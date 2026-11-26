"""KoeLog - 音声の取り出し（動画・音声の両方）

このモジュールが持つ責任:
  1. ファイルを ffmpeg に読ませて「読めるか／音声トラックが何本あるか／長さ」を知る
  2. 音声トラックを 16kHz / モノラル / ラウドネスノーマライズの WAV に取り出す
     （トラックが複数あれば **全部混ぜる**）
  3. 再生用の軽い音声コピー（mp3）を作る

**ffprobe は使わない。** 本番サーバーのアプリケーション制御ポリシーが
ffprobe.exe だけをブロックしている（WinError 4551）ため、情報はすべて
`ffmpeg -i` の標準エラーから読む。新しく ffprobe を呼ぶコードを足さないこと。

**タイムアウトで黙って続行しない。** 以前は前処理に 300 秒のタイムアウトがあり、
超えると例外が握りつぶされて「前処理なしの元ファイル」で続行していた。
数時間のファイルでは必ず超えるので、ここでは
  * 全体の時間制限は置かない（何時間かかってもよい）
  * そのかわり「ffmpeg の進捗が一定時間まったく進まない」ときだけ失敗させる
という形にしてある。失敗は MediaError として呼び出し側に伝わり、ジョブが
「失敗」になって理由が画面に出る。

--- 第2段階・第3段階で足す場所 ---
  * 第2段階（チャンク分割の文字起こし）: 下の `find_split_points()` を新設して、
    `extract_audio()` が作った WAV を約20分ごとの無音点で区切る。WAV は
    16kHz/モノラル/16bit 固定なので `wave` で先頭から読めばメモリは窓分だけで済む。
  * 第2段階（進捗表示）: `extract_audio(progress_cb=...)` がすでに
    「処理済みの秒数」をコールバックで渡す。取り出しフェーズの進捗率は
    ここに繋ぐだけでよい（呼び出し側で job に書く）。
  * 第3段階（話者分離）: 話者分離は **分割せず全体に1回** かけるので、
    `extract_audio()` の出力 WAV をそのまま渡す新関数 `diarize()` を
    別モジュール（diarize.py）に置く。このモジュールは触らなくてよい。
"""

import os
import re
import queue
import shutil
import subprocess
import threading
import time

import config


class MediaError(Exception):
    """利用者にそのまま見せる日本語の理由を持つ例外

    message: 画面に出す日本語（何が悪いか分かる文章）
    detail : ログ用の追加情報（ffmpeg の標準エラーの末尾など）
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


# ffmpeg が標準エラーに出す表記を読むための正規表現
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
# 例: "  Stream #0:1[0x2](jpn): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s"
_AUDIO_STREAM_RE = re.compile(r"^\s*Stream #\d+:\d+[^:]*:\s*Audio:", re.MULTILINE)
_VIDEO_STREAM_RE = re.compile(r"^\s*Stream #\d+:\d+[^:]*:\s*Video:", re.MULTILINE)
# -progress pipe:1 の出力。新しい ffmpeg は out_time_us も出す
_OUT_TIME_RE = re.compile(r"^out_time_(?:ms|us)=(-?\d+)\s*$")

# ffmpeg に許可するプロトコル。
# 入力がプレイリスト（.m3u8 など）だった場合に、そこから別の URL を
# 取りに行かせないため。ローカルのファイルとパイプだけ許す。
_PROTOCOL_ARGS = ["-protocol_whitelist", "file,pipe"]


def ffmpeg_available() -> bool:
    """ffmpeg が実行できるか（PATH 上、またはフルパス指定）"""
    p = config.FFMPEG_PATH
    if os.path.isabs(p):
        return os.path.exists(p)
    return shutil.which(p) is not None


def _hms_to_sec(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


class MediaInfo:
    """`ffmpeg -i` から読み取った、入力ファイルの素性"""

    def __init__(self, duration_sec: float, audio_streams: int,
                 video_streams: int, stderr_tail: str):
        self.duration_sec = duration_sec
        self.audio_streams = audio_streams
        self.video_streams = video_streams
        self.stderr_tail = stderr_tail

    @property
    def is_video(self) -> bool:
        return self.video_streams > 0


def _stderr_tail(text: str, lines: int = 6) -> str:
    """ffmpeg の標準エラーの末尾を、意味のある行だけ残して返す"""
    out = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return " / ".join(out[-lines:])


def _last_error_line(text: str) -> str:
    """失敗理由として一番それらしい1行を選ぶ"""
    out = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in reversed(out):
        # ffmpeg の案内行は理由にならないので飛ばす
        if ln.startswith("At least one output file") or ln.startswith("Use -h"):
            continue
        return ln
    return ""


def probe_media(path: str, timeout: int = 120) -> MediaInfo:
    """入力ファイルを ffmpeg に読ませて素性を返す。

    読めなければ MediaError。音声トラックが 0 本でもここでは失敗にしない
    （「音声が無い」の判定とメッセージは extract_audio 側でまとめて出す）。
    """
    cmd = ([config.FFMPEG_PATH, "-hide_banner", "-nostdin"]
           + _PROTOCOL_ARGS + ["-i", path])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise MediaError(
            "ffmpeg が見つかりません。サーバーに ffmpeg を用意してください。",
            f"FFMPEG_PATH={config.FFMPEG_PATH}")
    except subprocess.TimeoutExpired:
        raise MediaError(
            f"ファイルの情報を読み取れませんでした（{timeout}秒たっても ffmpeg が応答しません）。"
            "ファイルが壊れている可能性があります。",
            f"probe timeout path={path}")
    stderr = r.stderr or ""

    # 長さを聞くだけの呼び出しでは「出力ファイルが無い」で終了コード 1 が
    # 返るのが正常。読めたかどうかは Stream 行があるかで判断する。
    audio_n = len(_AUDIO_STREAM_RE.findall(stderr))
    video_n = len(_VIDEO_STREAM_RE.findall(stderr))
    duration = 0.0
    found = _DURATION_RE.findall(stderr)
    if found:
        duration = _hms_to_sec(*found[0])

    if audio_n == 0 and video_n == 0:
        # Stream 行が1つも無い＝ ffmpeg がこのファイルを入力として解釈できていない
        reason = _last_error_line(stderr) or "理由は ffmpeg から得られませんでした"
        raise MediaError(
            f"ffmpeg がこのファイルを読めませんでした: {reason}",
            _stderr_tail(stderr))

    return MediaInfo(duration, audio_n, video_n, _stderr_tail(stderr))


def _build_extract_cmd(input_path: str, output_path: str, audio_streams: int) -> list:
    """取り出し用の ffmpeg コマンドを組む。

    音声が1本のときは、**これまでの前処理とまったく同じフィルタ**
    （-ar 16000 / -ac 1 / -af loudnorm）を使う。既存の結果を変えないため。
    `-map 0:a:0` と `-vn` を足しているが、音声だけのファイルでは
    ffmpeg の既定の選択と同じ意味になる。

    音声が複数のときは amix で **全部混ぜる**。ffmpeg の既定は
    「チャンネル数が最も多い1本だけ」を選ぶので、参加者ごとにトラックが
    分かれた会議の録画では、黙って一部の人の声が落ちる。
    normalize=0 にして各トラックを 1/N に薄めないこと（薄めると小声の
    参加者がさらに小さくなる）。amix の出力は float のまま loudnorm に
    渡るので、ここで足し合わせても出力段まではクリップしない。
    """
    cmd = [config.FFMPEG_PATH, "-hide_banner", "-nostdin", "-y",
           "-progress", "pipe:1", "-nostats"]
    cmd += _PROTOCOL_ARGS
    cmd += ["-i", input_path]
    if audio_streams <= 1:
        cmd += ["-map", "0:a:0", "-vn", "-af", "loudnorm"]
    else:
        labels = "".join(f"[0:a:{i}]" for i in range(audio_streams))
        fc = (f"{labels}amix=inputs={audio_streams}:normalize=0[mix];"
              f"[mix]loudnorm[out]")
        cmd += ["-filter_complex", fc, "-map", "[out]", "-vn"]
    cmd += ["-ar", "16000", "-ac", "1", "-f", "wav", output_path]
    return cmd


def _run_ffmpeg_streaming(cmd: list, stall_sec: int, progress_cb=None,
                          label: str = "ffmpeg") -> str:
    """ffmpeg を起動し、進捗を見ながら終わるまで待つ。

    **全体の時間制限は置かない。** そのかわり、stall_sec 秒のあいだ
    進捗の出力が1行も来なければ「止まっている」と判断して kill し、
    MediaError を投げる。黙って続行は絶対にしない。

    戻り値は ffmpeg の出力（進捗行と標準エラーが混ざったもの）の末尾。
    """
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        raise MediaError(
            "ffmpeg が見つかりません。サーバーに ffmpeg を用意してください。",
            f"FFMPEG_PATH={config.FFMPEG_PATH}")

    lines: "queue.Queue[str]" = queue.Queue()
    # 標準エラーは全部は持たない（長時間だと膨らむ）。末尾だけ残す
    tail: "list[str]" = []

    def reader():
        try:
            for ln in proc.stdout:
                lines.put(ln.rstrip("\r\n"))
        finally:
            lines.put(None)   # 読み終わりの合図

    t = threading.Thread(target=reader, daemon=True, name=f"koelog-{label}-reader")
    t.start()

    last_activity = time.time()
    done = False
    while not done:
        try:
            ln = lines.get(timeout=5)
        except queue.Empty:
            if time.time() - last_activity > stall_sec:
                proc.kill()
                proc.wait(timeout=30)
                raise MediaError(
                    f"音声の取り出しが {stall_sec} 秒のあいだまったく進まなくなったため、"
                    "中止しました。ファイルが壊れているか、ディスクが応答していない"
                    "可能性があります。",
                    f"stalled: {' / '.join(tail[-6:])}")
            continue
        if ln is None:
            done = True
            continue
        last_activity = time.time()
        m = _OUT_TIME_RE.match(ln)
        if m:
            # out_time_ms / out_time_us はどちらもマイクロ秒で出る（ffmpeg の仕様）
            if progress_cb:
                try:
                    progress_cb(max(0.0, int(m.group(1)) / 1_000_000.0))
                except Exception:
                    pass
            continue
        if "=" in ln and not ln.startswith("["):
            # それ以外の進捗キー（frame=, speed= など）はログに残さない
            continue
        tail.append(ln)
        if len(tail) > 60:
            del tail[:-60]

    rc = proc.wait()
    out_tail = "\n".join(tail)
    if rc != 0:
        reason = _last_error_line(out_tail) or f"ffmpeg が終了コード {rc} で終わりました"
        raise MediaError(
            f"ffmpeg がこのファイルを処理できませんでした: {reason}",
            _stderr_tail(out_tail, 10))
    return out_tail


def extract_audio(input_path: str, output_path: str, info: MediaInfo,
                  progress_cb=None, stall_sec: int = None) -> str:
    """入力（音声でも動画でも）から 16kHz/モノラルの WAV を作って、そのパスを返す。

    音声トラックが無ければ MediaError。失敗したら必ず例外（前処理なしで
    続行することはしない）。
    """
    if info.audio_streams == 0:
        if info.is_video:
            raise MediaError(
                "この動画には音声トラックがありません（映像だけのファイルです）。"
                "無音の画面録画になっていないかご確認のうえ、音声が入ったファイルを"
                "アップロードしてください。",
                f"video_streams={info.video_streams}")
        raise MediaError(
            "このファイルには音声トラックがありません。"
            "音声が入ったファイルをアップロードしてください。",
            info.stderr_tail)

    if stall_sec is None:
        stall_sec = int(getattr(config, "FFMPEG_STALL_SEC", 600))
    cmd = _build_extract_cmd(input_path, output_path, info.audio_streams)
    _run_ffmpeg_streaming(cmd, stall_sec, progress_cb, label="extract")
    if not os.path.exists(output_path) or os.path.getsize(output_path) <= 44:
        # 44 バイト = WAV ヘッダだけ（中身が無い）
        raise MediaError(
            "音声を取り出せませんでした（出力が空です）。"
            "ファイルの音声トラックが壊れている可能性があります。",
            f"output={output_path}")
    return output_path


def make_playback_copy(wav_path: str, output_path: str,
                       stall_sec: int = None) -> str:
    """再生用の軽い音声コピー（mp3）を作る。

    元ファイル（数GBの動画）は処理後に消すので、プレビューの音声再生は
    このコピーを使う。取り出し済みの 16kHz/モノラル WAV から作るので、
    大きい元ファイルをもう一度デコードし直すことはない。

    形式を mp3 にしているのは、設計書が挙げていた Opus（ogg）だと
    一部のブラウザで再生できない可能性があり、オーナーのブラウザ環境が
    未確認だったため。既存の `/audio` も media_type を audio/mpeg と
    宣言しているので、こちらのほうが既存の動きに合う。
    モノラル 16kHz / 32kbps で 1時間あたり約14MB。
    """
    if stall_sec is None:
        stall_sec = int(getattr(config, "FFMPEG_STALL_SEC", 600))
    cmd = [config.FFMPEG_PATH, "-hide_banner", "-nostdin", "-y",
           "-progress", "pipe:1", "-nostats"]
    cmd += _PROTOCOL_ARGS
    cmd += ["-i", wav_path, "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", "32k", "-f", "mp3", output_path]
    _run_ffmpeg_streaming(cmd, stall_sec, None, label="playback")
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise MediaError("再生用の音声コピーを作れませんでした。", f"output={output_path}")
    return output_path
