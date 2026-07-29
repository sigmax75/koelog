@echo off
chcp 65001 >nul
echo ========================================
echo   KoeLog インストーラー
echo ========================================
echo.

REM Python確認
python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Pythonが見つかりません。
    echo Python 3.9以上をインストールしてください。
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python確認済み
python --version

REM 仮想環境作成
if not exist "venv" (
    echo.
    echo [1/3] 仮想環境を作成中...
    python -m venv venv
    echo [OK] 仮想環境作成完了
) else (
    echo [OK] 仮想環境は既に存在します
)

REM パッケージインストール
echo.
echo [2/3] パッケージをインストール中...
echo       （初回はWhisperモデルのダウンロードに時間がかかります）
venv\Scripts\pip install -r requirements.txt
if errorlevel 1 (
    echo [エラー] パッケージのインストールに失敗しました。
    pause
    exit /b 1
)
echo [OK] パッケージインストール完了

REM ffmpeg確認+ダウンロード
echo.
echo [3/3] ffmpegを確認中...
where ffmpeg >nul 2>&1
if not errorlevel 1 (
    echo [OK] ffmpegはPATHに存在します
    goto :ffmpeg_done
)

if exist "ffmpeg\bin\ffmpeg.exe" (
    echo [OK] ffmpegは既にダウンロード済みです
    goto :ffmpeg_done
)

echo       ffmpegをダウンロード中...
if not exist "ffmpeg" mkdir ffmpeg

REM curlでダウンロード（Windows 10以降は標準搭載）
curl -L -o ffmpeg\ffmpeg.zip https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip
if errorlevel 1 (
    echo [警告] ffmpegのダウンロードに失敗しました。
    echo        ffmpegなしでも動作しますが、精度向上の前処理が無効になります。
    goto :ffmpeg_done
)

echo       展開中...
powershell -Command "Expand-Archive -Path 'ffmpeg\ffmpeg.zip' -DestinationPath 'ffmpeg\temp' -Force"

REM 展開されたフォルダからbinを移動
for /d %%d in (ffmpeg\temp\ffmpeg-*) do (
    if exist "%%d\bin" (
        xcopy "%%d\bin\*" "ffmpeg\bin\" /E /Y /Q >nul
    )
)

REM 後片付け
del ffmpeg\ffmpeg.zip 2>nul
rmdir /s /q ffmpeg\temp 2>nul

if exist "ffmpeg\bin\ffmpeg.exe" (
    echo [OK] ffmpegインストール完了
) else (
    echo [警告] ffmpegの展開に失敗しました。手動でインストールしてください。
)

:ffmpeg_done

echo.
echo ========================================
echo   インストール完了！
echo ========================================
echo.
echo   起動方法: start.bat をダブルクリック
echo   ブラウザ: http://localhost:8000
echo.
pause
