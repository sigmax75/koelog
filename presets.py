"""文字起こしの「精度と時間のバランス」プリセット（5段階）

段階3 は 2026-11-18 時点の現行設定をそのまま写したもの。
（config.MODEL_SIZE / COMPUTE_TYPE と app.run_transcription() の transcribe 引数）
**段階3 の中身は変更しないこと。** オーナーが「今の状態」を基準にしているため。

server_ratio は「本番サーバー（DESKTOP-0SMA4J2 / Ryzen 5 PRO 4650G 6C12T / GPU無し）で
音声長の何倍の実時間がかかるか」の見込み。段階ごとの根拠は下のコメントに書いてある。

この見込みは検証用PC（ThinkPad E480 / i5-8250U 4C8T / RAM 8GB）での実測からの換算で、
実機では必ずずれる。そのため実際のジョブの所要時間から倍率を学習し
（quality_stats / update_stats）、2本たまったら表示をそちらに切り替える。
"""

import os

DEFAULT_LEVEL = "3"
LEVELS = ["1", "2", "3", "4", "5"]

# 段階3（現行）の transcribe 引数。**1つも足さない・引かない。**
# best_of は現行コードが渡していないので、ここでも渡さない。
_LEVEL3_TRANSCRIBE = {
    "beam_size": 5,
    "vad_filter": True,
    "vad_parameters": {
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 300,
    },
    "temperature": 0.0,
    "condition_on_previous_text": False,
    "hallucination_silence_threshold": 2.0,
    "repetition_penalty": 1.1,
    "no_repeat_ngram_size": 3,
}

# 段階の設計方針（2026-11-18 の実測にもとづく。根拠はこのすぐ下に書いてある）
#
# 【実測の限界】精度の比較に使った「正解」は、文字起こしの出力に人が修正を
# 入れたものであって、独立した正解ではない。元の出力が丸ごと落とした発話は
# 正解側にも載っていないため、ここの数字は取りこぼしを小さめに見積もっている。
#
# 実測して分かったこと:
#   * 効くのはモデルの大きさだけだった。CER は base 64.9% / small 33.7% /
#     medium 20.9% と、モデルを大きくするほど素直に下がった。
#   * beam_size はほぼ効かなかった。small は beam1 で 33.7%、beam5 で 31.0%。
#     medium は beam1 で 20.9%、beam5（現行）で 23.5%。差は測定のばらつきの範囲。
#     → beam は「精度のつまみ」ではなく「時間のつまみ」として使う。
#   * vad_filter を切ると少し悪くなった（medium beam5 で 25.9% → 29.5%、脱落 2件→3件）。
#     → 全段階で現行と同じ vad 設定のままにする。速度のためにも使わない。
#   * temperature のフォールバック [0.0,0.2,0.4] は大きく悪化させた
#     （beam8 と併用で CER 50.9%、脱落 2件→7件）。→ 全段階で 0.0 固定。
#
# したがって段階は「モデルの大きさ」を主軸に、beam を副軸にして組む。
_COMMON_VAD = {"min_silence_duration_ms": 500, "speech_pad_ms": 300}


def _tr(beam_size, best_of=None):
    """段階3 と同じ土台で beam だけ変えた transcribe 引数を作る"""
    d = {
        "beam_size": beam_size,
        "vad_filter": True,
        "vad_parameters": dict(_COMMON_VAD),
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "hallucination_silence_threshold": 2.0,
        "repetition_penalty": 1.1,
        "no_repeat_ngram_size": 3,
    }
    if best_of is not None:
        d["best_of"] = best_of
    return d


PRESETS = {
    "1": {
        "name": "急ぎ（下書き）",
        "summary": "時間優先。小さいモデルを使います。話の流れは追えますが、"
                   "固有名詞や専門用語の誤りと聞き落としが増えます（聞き直す前提の下書き向け）。",
        "model_size": "small",
        "compute_type": "int8",
        "cpu_threads": 0,
        "transcribe": _tr(1, best_of=1),
        "server_ratio": 0.17,
        "model_mem_mb": 750,
        "ratio_measured": True,
    },
    "2": {
        "name": "速い",
        # 精度は実測した。時間（beam を下げてどれだけ短くなるか）は未実測。
        # 1文に混ぜると「実測では」が両方に掛かって読めるので分けて書く。
        "summary": "標準と同じモデルで探索を浅くしたもの。精度は実測で標準と同程度でした。"
                   "時間は標準より短くなる見込みですが、短縮量はまだ測れていません。",
        "model_size": "medium",
        "compute_type": "int8",
        "cpu_threads": 0,
        "transcribe": _tr(1, best_of=1),
        "server_ratio": 0.56,
        # beam を下げたときの短縮率は検証PCでは測れなかった（下のコメント参照）
        "ratio_measured": False,
        "model_mem_mb": 1800,
    },
    "3": {
        "name": "標準（これまでと同じ）",
        "summary": "これまでの KoeLog と同じ設定。迷ったらこれ。",
        "model_size": "medium",
        "compute_type": "int8",
        "cpu_threads": 0,
        "transcribe": dict(_LEVEL3_TRANSCRIBE),
        "server_ratio": 0.70,
        "model_mem_mb": 1800,
        "ratio_measured": True,
    },
    "4": {
        "name": "高精度",
        "summary": "一番大きいモデルを使い、探索は浅くします。標準より聞き落としと"
                   "固有名詞の誤りが減る見込みです。初回はモデルの取得に約3.1GBの"
                   "ダウンロードが要ります。",
        "model_size": "large-v3",
        "compute_type": "int8",
        "cpu_threads": 0,
        "transcribe": _tr(1, best_of=1),
        "server_ratio": 1.25,
        "model_mem_mb": 3400,
        # large-v3 は検証PC（RAM 8GB・空き約1GB）では測れなかった。
        # モデルの読み込みは成功（149秒）したが、30秒ぶんの文字起こしが
        # 9分以上かかっても終わらず、ページングで止まった。
        # 下の倍率は medium の実測からモデルの大きさで換算した見込み。
        "ratio_measured": False,
    },
    "5": {
        "name": "最高精度（時間がかかります）",
        "summary": "一番大きいモデルで探索も深くします。1時間の会議で1時間半以上かかる"
                   "見込みなので、急がない録音に向きます。",
        "model_size": "large-v3",
        "compute_type": "int8",
        "cpu_threads": 0,
        "transcribe": _tr(5, best_of=5),
        "server_ratio": 1.55,
        "model_mem_mb": 3400,
        "ratio_measured": False,
    },
}

# モデルごとの初回ダウンロード量（目安・GB）。UI の注意書き用
MODEL_DOWNLOAD_GB = {
    "tiny": 0.08,
    "base": 0.15,
    "small": 0.5,
    "medium": 1.5,
    "large-v3": 3.1,
}


def normalize_level(value) -> str:
    """フォームや設定から来た値を正しい段階名に直す。不正なら既定段階"""
    s = str(value).strip() if value is not None else ""
    return s if s in PRESETS else DEFAULT_LEVEL


def get(level) -> dict:
    """段階の定義を返す（存在しない段階なら既定段階）"""
    return PRESETS[normalize_level(level)]


# 実績から倍率を学ぶときに必要な最低本数。これ未満は組み込みの見込みを使う
MIN_SAMPLES_FOR_LEARNED = 2

# 実績として数える音声の最低の長さ（秒）。
# これより短い音声では、モデルの読み込みと ffmpeg 前処理という
# 「音声の長さに比例しない固定費」が所要時間の大半を占めてしまう。
# 実測: 8秒の音声で倍率 5.04（＝1時間で5時間という表示になった）。
# 会議の録音は数分〜1時間なので、短いものは学習に使わない。
MIN_DURATION_FOR_LEARNING = 120.0

# 実績として受け付ける倍率の範囲。これを外れた回は測り間違いとして捨てる
RATIO_SANE_MIN = 0.01
RATIO_SANE_MAX = 20.0


def effective_ratio(level, stats=None) -> float:
    """この段階の「音声長に対する倍率」。

    実績（stats）が十分たまっていればそれを使う。たまっていなければ
    組み込みの見込み（server_ratio）を使う。

    組み込みの見込みは検証用PC（8GB・4コア）の実測からの換算で、
    本番サーバーの実機では必ずずれる。実績で上書きできるようにしてある。
    """
    lv = normalize_level(level)
    base = PRESETS[lv]["server_ratio"]
    if not stats:
        return base
    s = stats.get(lv) if isinstance(stats, dict) else None
    if not isinstance(s, dict):
        return base
    try:
        n = int(s.get("n", 0))
        r = float(s.get("ratio", 0))
    except (TypeError, ValueError):
        return base
    if n >= MIN_SAMPLES_FOR_LEARNED and r > 0:
        return r
    return base


def estimate_sec(duration_sec: float, level, stats=None) -> float:
    """音声長から所要時間の見込み（秒）を出す"""
    try:
        d = float(duration_sec or 0)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0:
        return 0.0
    return round(d * effective_ratio(level, stats), 0)


def hour_minutes(level, stats=None) -> int:
    """1時間の会議でおよそ何分かかるか（分・整数）"""
    return int(round(60 * effective_ratio(level, stats)))


def update_stats(stats: dict, level, duration_sec: float, elapsed_sec: float) -> dict:
    """1本終わるごとに倍率の実績を更新した新しい dict を返す（移動平均）。

    直近に重みを置きたいので、件数が増えても古い値に引っぱられすぎない
    ように重みの下限を 1/8 で止める。
    """
    lv = normalize_level(level)
    try:
        d = float(duration_sec or 0)
        e = float(elapsed_sec or 0)
    except (TypeError, ValueError):
        return stats or {}
    out = dict(stats or {})
    if d <= 0 or e <= 0:
        return out       # 音声長が取れなかった回は学習に使わない
    if d < MIN_DURATION_FOR_LEARNING:
        return out       # 短すぎる音声は固定費に埋もれるので使わない
    ratio = e / d
    if not (RATIO_SANE_MIN <= ratio <= RATIO_SANE_MAX):
        return out       # ありえない値は捨てる
    cur = out.get(lv) if isinstance(out.get(lv), dict) else None
    if not cur:
        out[lv] = {"n": 1, "ratio": round(ratio, 4)}
        return out
    try:
        n = int(cur.get("n", 0))
        prev = float(cur.get("ratio", 0))
    except (TypeError, ValueError):
        out[lv] = {"n": 1, "ratio": round(ratio, 4)}
        return out
    if n <= 0 or prev <= 0:
        out[lv] = {"n": 1, "ratio": round(ratio, 4)}
        return out
    w = max(1.0 / 8.0, 1.0 / (n + 1))
    out[lv] = {"n": n + 1, "ratio": round(prev * (1 - w) + ratio * w, 4)}
    return out


def download_gb(level) -> float:
    return MODEL_DOWNLOAD_GB.get(get(level)["model_size"], 0.0)


def _hf_cache_roots():
    """faster-whisper が使う Hugging Face キャッシュの候補を返す"""
    roots = []
    for env in ("HF_HUB_CACHE",):
        v = os.environ.get(env)
        if v:
            roots.append(v)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        roots.append(os.path.join(hf_home, "hub"))
    roots.append(os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub"))
    out = []
    for r in roots:
        if r and r not in out:
            out.append(r)
    return out


def model_is_downloaded(model_size: str) -> bool:
    """そのモデルが既にローカルに取得済みか（ネットワークに出ない）。

    ローカルのフォルダを model_size に指定する運用もあるので、
    実在するフォルダに model.bin があればそれも取得済みとみなす。
    """
    if not model_size:
        return False
    if os.path.isdir(model_size) and os.path.exists(os.path.join(model_size, "model.bin")):
        return True
    repo_dir = f"models--Systran--faster-whisper-{model_size}"
    for root in _hf_cache_roots():
        base = os.path.join(root, repo_dir, "snapshots")
        if not os.path.isdir(base):
            continue
        try:
            for snap in os.listdir(base):
                if os.path.exists(os.path.join(base, snap, "model.bin")):
                    return True
        except OSError:
            continue
    return False


def model_status(stats=None):
    """段階ごとのモデル取得状況。管理画面の表示用"""
    out = []
    seen = {}
    for lv in LEVELS:
        p = PRESETS[lv]
        size = p["model_size"]
        if size not in seen:
            seen[size] = model_is_downloaded(size)
        s = (stats or {}).get(lv) if isinstance(stats, dict) else None
        n = 0
        if isinstance(s, dict):
            try:
                n = int(s.get("n", 0))
            except (TypeError, ValueError):
                n = 0
        out.append({
            "level": lv,
            "name": p["name"],
            "model_size": size,
            "downloaded": seen[size],
            "download_gb": MODEL_DOWNLOAD_GB.get(size, 0.0),
            "model_mem_mb": p.get("model_mem_mb", 0),
            "hour_minutes": hour_minutes(lv, stats),
            "builtin_hour_minutes": int(round(60 * p["server_ratio"])),
            "samples": n,
            "learned": n >= MIN_SAMPLES_FOR_LEARNED,
            "ratio_measured": p.get("ratio_measured", True),
            "time_basis": time_basis(lv, stats),
        })
    return out


def time_basis(level, stats=None) -> str:
    """その段階の「1時間の会議でおよそN分」が何に基づく数字かを一言で返す。

    アップロード画面でも管理画面でも同じ言い方になるよう、ここ1か所で決める。
    未実測のものを実測のように見せない。
    """
    lv = normalize_level(level)
    s = (stats or {}).get(lv) if isinstance(stats, dict) else None
    n = 0
    if isinstance(s, dict):
        try:
            n = int(s.get("n", 0))
        except (TypeError, ValueError):
            n = 0
    if n >= MIN_SAMPLES_FOR_LEARNED:
        return f"このサーバーでの実績（{n}本）"
    if not PRESETS[lv].get("ratio_measured", True):
        return "未実測の見込み"
    return "見込み"


def ui_levels(stats=None):
    """アップロード画面の選択肢用（名前・説明・1時間の目安）"""
    return [{
        "level": lv,
        "name": PRESETS[lv]["name"],
        "summary": PRESETS[lv]["summary"],
        "hour_minutes": hour_minutes(lv, stats),
        "model_size": PRESETS[lv]["model_size"],
        "downloaded": model_is_downloaded(PRESETS[lv]["model_size"]),
        "download_gb": MODEL_DOWNLOAD_GB.get(PRESETS[lv]["model_size"], 0.0),
        # 所要時間が実測か見込みか。画面に出さないと、未実測の数字が
        # 実測のように見えてしまう
        "ratio_measured": PRESETS[lv].get("ratio_measured", True),
        "time_basis": time_basis(lv, stats),
    } for lv in LEVELS]
