"""KoeLog - 分割アップロードの受け皿（断片の保存・再開・掃除）

ブラウザから小さく分けて送られてくるファイルを、保存先の
`<upload_dir>/.koelog_parts/` の下に組み立てる。

設計の要点:
  * **受信済みのバイト数は `.part` ファイルの実サイズを正とする。**
    JSON 側の数字は参考情報。こうしておくと、サーバーが途中で落ちても
    「どこまで届いたか」が食い違わない。
  * 断片は **オフセットを照合してから追記** する。ずれていたら 409 を返し、
    ブラウザに正しい位置を教えて続きから送らせる（重複も欠落も起きない）。
  * 1断片ごとにハッシュ（SHA-256 または CRC32）を照合する。ブラウザが
    `crypto.subtle` を使えない環境（http のまま LAN の IP で開いた場合）
    でも CRC32 なら必ず計算できるので、どちらか必ず付く。
  * 中断されたまま放置された断片は、起動時と1日1回の掃除で消える。

--- 第2段階・第3段階で足す場所 ---
  * 第2段階（ジョブの永続化）: このモジュールと同じ「一時ファイルに書いて
    os.replace」の作り（`_atomic_write_json`）をそのまま `jobstore.py` で
    使う。アップロードの断片とジョブの状態は別ファイルに分けること
    （断片は保存先ドライブ、ジョブは結果保存先ドライブ）。
"""

import hashlib
import json
import os
import re
import time
import uuid
import zlib

PARTS_DIRNAME = ".koelog_parts"
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# meta に立てる印。True のあいだ「ディスクの空きの予約」から外れる。
# **断片そのものは残る**（続きから再開できる）。再開の受付（init）で下ろす。
RELEASED_KEY = "reservation_released"


class UploadError(Exception):
    """利用者に見せる日本語の理由と HTTP ステータスを持つ例外"""

    def __init__(self, message: str, status_code: int = 400, extra: dict = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}


def new_upload_id() -> str:
    return uuid.uuid4().hex


def valid_upload_id(upload_id: str) -> bool:
    """パスに混ぜる値なので、32桁の16進数以外は受け付けない"""
    return bool(_UPLOAD_ID_RE.match(upload_id or ""))


def parts_dir(upload_dir: str) -> str:
    d = os.path.join(upload_dir, PARTS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _meta_path(upload_dir: str, upload_id: str) -> str:
    return os.path.join(parts_dir(upload_dir), f"{upload_id}.json")


def part_path(upload_dir: str, upload_id: str) -> str:
    return os.path.join(parts_dir(upload_dir), f"{upload_id}.part")


def _atomic_write_json(path: str, data: dict):
    """一時ファイルに書いて os.replace で置き換える（書きかけを読ませない）"""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def save_meta(upload_dir: str, meta: dict):
    meta["updated_at"] = time.time()
    _atomic_write_json(_meta_path(upload_dir, meta["upload_id"]), meta)


def load_meta(upload_dir: str, upload_id: str) -> dict:
    if not valid_upload_id(upload_id):
        raise UploadError("アップロードIDの形式が正しくありません", 400)
    p = _meta_path(upload_dir, upload_id)
    if not os.path.exists(p):
        raise UploadError(
            "このアップロードの受付情報が見つかりません（時間がたって片付けられた可能性があります）。"
            "お手数ですが、もう一度ファイルを選んでください。", 404)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise UploadError(f"アップロードの受付情報を読めません: {e}", 500)


def received_bytes(upload_dir: str, upload_id: str) -> int:
    """いまサーバーに届いているバイト数（.part の実サイズが正）"""
    p = part_path(upload_dir, upload_id)
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def find_by_resume_key(upload_dir: str, resume_key: str):
    """同じファイルの途中まで届いた受付があれば、その meta を返す。

    resume_key はブラウザが作る「ファイル名｜サイズ｜最終更新日時」。
    ブラウザを閉じて開き直し、同じファイルを選び直したときに
    続きから送れるようにするためのもの。
    """
    if not resume_key:
        return None
    d = parts_dir(upload_dir)
    best = None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        if meta.get("resume_key") != resume_key:
            continue
        if not os.path.exists(part_path(upload_dir, meta.get("upload_id", ""))):
            continue
        if best is None or meta.get("updated_at", 0) > best.get("updated_at", 0):
            best = meta
    return best


def pending_reserved_bytes(upload_dir: str, exclude_upload_id: str = "",
                           ttl_hours: float = 0.0) -> int:
    """受付済みで未完了のアップロードが「これから書き込む予定」のバイト数の合計。

    なぜ必要か（2026-11-26 修正）:
      ディスクの空き検査が `shutil.disk_usage().free` だけを見ていたため、
      2本の `/upload/init` を同時に投げると、どちらも「空いている」と判断して
      両方受け付けてしまい、空きを二重取りしていた（実測: 空き13.3GB に
      9.3GB × 2本が 200 で通った）。送信途中で実際に空きが尽きると
      `PUT /chunk` が 507 になり、送信にかけた時間が丸ごと無駄になる。

    数え方:
      * **残り（size - 受信済み）だけを数える。** すでに書き込んだぶんは
        `free` に反映されているので、二重に引かない
      * 状態は `save_meta` が書いた JSON と `.part` の実サイズから数える。
        予約専用の持ち物を新設しない（再起動で予約だけ消える穴を作らない）
      * `.part` が無いもの（完了・取消・破棄済み）は数えない。
        `delete` / `finish` が meta と .part を消すので、予約も自然に外れる
      * `RELEASED_KEY` が立っているもの（送信が失敗して画面が予約を手放した
        もの）は数えない。**断片は残っているので続きから再開できる**が、
        その先を書く約束はもう持っていないので予約には入れない
      * 更新が `ttl_hours` より古いものは数えない（`cleanup_stale` が
        消す対象なので、予約に残すと使えるはずの空きが永久に減る）

    exclude_upload_id: 再開（resume）の検査中は、その受付自身を除く。
      呼び出し側が `already_received` を差し引いて必要量を出しているため、
      自分のぶんを予約として足すと二重に引いてしまう。
    """
    d = os.path.join(upload_dir, PARTS_DIRNAME)
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    limit = 0.0
    if ttl_hours and ttl_hours > 0:
        limit = time.time() - ttl_hours * 3600
    total = 0
    for name in names:
        m = re.match(r"^([0-9a-f]{32})\.json$", name)
        if not m:
            continue
        upload_id = m.group(1)
        if exclude_upload_id and upload_id == exclude_upload_id:
            continue
        part = part_path(upload_dir, upload_id)
        try:
            received = os.path.getsize(part)
        except OSError:
            # .part が無い = 完了・取消・破棄済み。予約しない
            continue
        meta_p = os.path.join(d, name)
        if limit:
            newest = 0.0
            for p in (part, meta_p):
                try:
                    newest = max(newest, os.path.getmtime(p))
                except OSError:
                    pass
            if newest and newest < limit:
                continue
        try:
            with open(meta_p, "r", encoding="utf-8") as f:
                meta = json.load(f)
            size = int(meta.get("size", 0))
        except Exception:
            continue
        if meta.get(RELEASED_KEY):
            continue
        remaining = size - received
        if remaining > 0:
            total += remaining
    return total


def create(upload_dir: str, filename: str, size: int, resume_key: str,
           ext: str) -> dict:
    """新しい受付を作る（.part を空で作成）"""
    upload_id = new_upload_id()
    meta = {
        "upload_id": upload_id,
        "filename": filename,
        "ext": ext,
        "size": int(size),
        "resume_key": resume_key or "",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with open(part_path(upload_dir, upload_id), "wb"):
        pass
    save_meta(upload_dir, meta)
    return meta


def verify_chunk(data: bytes, sha256_hex: str = "", crc32_hex: str = ""):
    """断片のハッシュを照合する。どちらも無ければ照合しない（ok=False を返す）"""
    if sha256_hex:
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != sha256_hex.strip().lower():
            raise UploadError(
                "送られてきたデータが途中で壊れていました（この断片は破棄しました）。"
                "自動で送り直します。", 422)
        return "sha256"
    if crc32_hex:
        actual = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
        if actual != crc32_hex.strip().lower().zfill(8):
            raise UploadError(
                "送られてきたデータが途中で壊れていました（この断片は破棄しました）。"
                "自動で送り直します。", 422)
        return "crc32"
    return ""


def append_chunk(upload_dir: str, upload_id: str, offset: int, data: bytes) -> int:
    """断片を追記して、追記後の受信済みバイト数を返す。

    オフセットが合わないときは 409 と現在位置を返す（ブラウザが直して送り直す）。
    """
    meta = load_meta(upload_dir, upload_id)
    p = part_path(upload_dir, upload_id)
    if not os.path.exists(p):
        raise UploadError(
            "受信中のファイルが見つかりません。もう一度ファイルを選んでください。", 404)
    current = os.path.getsize(p)
    if offset != current:
        raise UploadError(
            f"送信位置がずれています（サーバーは {current} バイトまで受け取っています）。"
            "この位置から送り直します。",
            409, {"received": current, "size": meta.get("size", 0)})
    size = int(meta.get("size", 0))
    if size and current + len(data) > size:
        raise UploadError(
            f"宣言されたサイズ（{size} バイト）を超えるデータが送られました。", 400,
            {"received": current, "size": size})
    with open(p, "ab") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    save_meta(upload_dir, meta)
    return os.path.getsize(p)


def sha256_file(path: str, block_size: int = 1024 * 1024) -> str:
    """ファイル全体の SHA-256（1MBずつ読むのでメモリは一定）"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(block_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def finish(upload_dir: str, upload_id: str, dest_path: str) -> dict:
    """受信完了。サイズを照合してから本来の置き場所へ移し、ハッシュを返す。

    「繋いだあとファイルが壊れていないこと」の確認は2段構え。
      1. 断片ごとのハッシュ照合（append_chunk の前に verify_chunk）
      2. ここで宣言サイズと実サイズの一致を確認し、全体の SHA-256 を計算して返す
    """
    meta = load_meta(upload_dir, upload_id)
    p = part_path(upload_dir, upload_id)
    if not os.path.exists(p):
        raise UploadError("受信中のファイルが見つかりません。", 404)
    actual = os.path.getsize(p)
    size = int(meta.get("size", 0))
    if size and actual != size:
        raise UploadError(
            f"受信したサイズが合いません（届いた {actual} バイト / 送る予定 {size} バイト）。"
            "送信が途中で終わっています。",
            409, {"received": actual, "size": size})
    digest = sha256_file(p)
    os.replace(p, dest_path)
    try:
        os.remove(_meta_path(upload_dir, upload_id))
    except OSError:
        pass
    meta["sha256"] = digest
    meta["final_size"] = actual
    return meta


def release(upload_dir: str, upload_id: str) -> bool:
    """**予約だけ**手放す（断片は残す）。手放せたら True。

    なぜ「消す」ではなく「手放す」なのか（2026-11-26 追加）:
      送信が失敗したときに `delete` で断片ごと消すと、同じファイルを
      上げ直したときに**続きから再開できなくなり、また全部送り直し**になる。
      オーナーの要望（放り込めば終わる・手作業をなくす）に対しては、
      再開できることのほうが効く。
      一方、予約を持ったままにすると、失敗した送信が最長
      `UPLOAD_PART_TTL_HOURS`（既定24時間）ぶん他のアップロードを
      ブロックしうる。そこで**「断片は残す／予約だけ外す」**を用意した。

    再開の受付（app.upload_init）が印を下ろし、そのとき改めて空きを検査する。
    """
    if not valid_upload_id(upload_id):
        return False
    try:
        meta = load_meta(upload_dir, upload_id)
    except UploadError:
        return False
    if not os.path.exists(part_path(upload_dir, upload_id)):
        # 断片が無いなら残すものが無い。meta だけ片付ける
        delete(upload_dir, upload_id)
        return False
    meta[RELEASED_KEY] = True
    save_meta(upload_dir, meta)
    return True


def reactivate(upload_dir: str, meta: dict) -> dict:
    """手放していた予約を戻す（再開の受付で、空きの検査に通ったあとに呼ぶ）"""
    if meta.get(RELEASED_KEY):
        meta = dict(meta)
        meta[RELEASED_KEY] = False
        save_meta(upload_dir, meta)
    return meta


def delete(upload_dir: str, upload_id: str):
    """受付を破棄（中断・取消）。**断片ごと消す**（続きから再開はできなくなる）"""
    if not valid_upload_id(upload_id):
        return
    for p in (part_path(upload_dir, upload_id), _meta_path(upload_dir, upload_id)):
        try:
            os.remove(p)
        except OSError:
            pass


def cleanup_stale(upload_dir: str, ttl_hours: float) -> int:
    """更新が ttl_hours より古い断片を消す。消した受付の数を返す。

    中断されたまま放置された断片がディスクに残り続けないようにするための掃除。
    起動時に1回と、1日1回呼ぶ。
    """
    removed = 0
    try:
        d = parts_dir(upload_dir)
        names = os.listdir(d)
    except OSError:
        return 0
    limit = time.time() - ttl_hours * 3600
    ids = set()
    for name in names:
        m = re.match(r"^([0-9a-f]{32})\.(json|part|json\.tmp)$", name)
        if m:
            ids.add(m.group(1))
    for upload_id in ids:
        newest = 0.0
        for p in (part_path(upload_dir, upload_id), _meta_path(upload_dir, upload_id)):
            try:
                newest = max(newest, os.path.getmtime(p))
            except OSError:
                pass
        if newest and newest < limit:
            delete(upload_dir, upload_id)
            removed += 1
    return removed
