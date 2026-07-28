import os

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
RESULT_DIR = os.path.join(os.path.dirname(__file__), "results")
MODEL_SIZE = "small"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
LANGUAGE = "ja"
MAX_FILE_SIZE_MB = 500
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm"}
