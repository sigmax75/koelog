@echo off
chcp 65001 >nul
echo ========================================
echo   KoeLog 起動中...
echo ========================================
echo.

REM 仮想環境確認
if not exist "venv\Scripts\python.exe" (
    echo [エラー] 仮想環境が見つかりません。
    echo 先に install.bat を実行してください。
    pause
    exit /b 1
)

REM ffmpegをPATHに追加（ローカルインストールの場合）
if exist "ffmpeg\bin\ffmpeg.exe" (
    set "PATH=%~dp0ffmpeg\bin;%PATH%"
    echo [OK] ffmpeg: ローカル版を使用
) else (
    where ffmpeg >nul 2>&1
    if not errorlevel 1 (
        echo [OK] ffmpeg: システム版を使用
    ) else (
        echo [注意] ffmpegが見つかりません（前処理なしで動作します）
    )
)

echo.
echo   ブラウザで http://localhost:8000 にアクセスしてください
echo   終了するにはこのウィンドウを閉じてください
echo.

REM サーバー起動
venv\Scripts\python -m uvicorn app:app --host 0.0.0.0 --port 8000
