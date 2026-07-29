import os

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
RESULT_DIR = os.path.join(os.path.dirname(__file__), "results")
MODEL_SIZE = "small"
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
INITIAL_PROMPT = "会議の議事録です。ビジネス用語、専門用語が含まれます。"
TEMPERATURE = 0.0

# --- ffmpeg ---
FFMPEG_PATH = "ffmpeg"  # パスが通っていない場合はフルパスを指定
