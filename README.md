# KoeLog

会議録音の文字起こしツール。ブラウザから音声をアップロードするだけで、タイムスタンプ付きテキストに変換します。

## セットアップ

```bash
pip install -r requirements.txt
```

## 起動

```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

初回起動時にWhisperモデル（約500MB）がダウンロードされます。

## 使い方

1. ブラウザで http://localhost:8000 にアクセス
2. 音声ファイルをドラッグ&ドロップまたは選択
3. 「文字起こし開始」をクリック
4. 完了したら結果をダウンロード

## 対応形式

mp3, wav, m4a, ogg, flac, webm

## 動作環境

- Python 3.9+
- CPU動作（GPU不要）
- メモリ: 4GB以上推奨
