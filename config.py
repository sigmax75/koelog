import os

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
RESULT_DIR = os.path.join(os.path.dirname(__file__), "results")
MODEL_SIZE = "medium"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
LANGUAGE = "ja"
MAX_FILE_SIZE_MB = 500
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm"}

# --- メール通知 ---
SMTP_HOST = ""          # 例: "smtp.gmail.com"
SMTP_PORT = 587         # TLS: 587, SSL: 465
SMTP_USER = ""          # 例: "user@gmail.com"
SMTP_PASSWORD = ""      # 例: Gmailならアプリパスワード
SMTP_FROM = ""          # 送信元アドレス（未設定ならSMTP_USERが使われる）
SMTP_USE_TLS = True
MAIL_ENABLED = False    # TrueにするとSMTP設定が有効になる

# --- Whisper精度設定 ---
INITIAL_PROMPT = "会議の議事録です。ビジネス用語、医療用語、IT用語が含まれます。アレルギー、抗生剤、ばい菌、処方、診断、症状、治療、検査、手術、入院、退院、カルテ、レセプト、サーバー、データベース、デプロイ、リリース、スプリント、バックログ。"

# Field-specific prompt presets
PROMPT_PRESETS = {
    "general": "会議の議事録です。ビジネス用語が含まれます。予算、売上、利益、コスト、戦略、施策、KPI、ROI。",
    "medical": "医療の会話です。医療用語、薬品名が含まれます。アレルギー、抗生剤、ばい菌、処方、診断、症状、治療、検査、手術、入院、退院、カルテ、レセプト、服薬、副作用、炎症、感染。",
    "it": "IT関連の会議です。技術用語が含まれます。サーバー、データベース、デプロイ、リリース、スプリント、バックログ、API、マイクロサービス、コンテナ、CI/CD、クラウド、セキュリティ。",
    "legal": "法務・契約に関する会話です。契約書、条項、瑕疵、損害賠償、免責、守秘義務、NDA、コンプライアンス、規約、法令遵守。",
    "finance": "財務・会計に関する会議です。決算、貸借対照表、損益計算書、キャッシュフロー、償却、引当金、税務、監査、予実管理。",
    "custom": "",
}
TEMPERATURE = 0.0

# --- ffmpeg ---
FFMPEG_PATH = "ffmpeg"  # パスが通っていない場合はフルパスを指定
