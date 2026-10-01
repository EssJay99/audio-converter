@echo off
REM Build the Audio Converter Windows installer. Run ON Windows:
REM   scripts\build_windows.bat [version]
REM Output: dist\AudioConverter-Setup-<version>.exe
REM
REM For the person installing: run the Setup file, click through the
REM wizard, launch from the Start menu. Everything needed is inside.
REM
REM Code signing (kills SmartScreen warnings) is automatic when these
REM environment variables are set (CI secrets of the same names):
REM   WINDOWS_CERT_BASE64    PFX certificate, base64-encoded
REM   WINDOWS_CERT_PASSWORD  PFX password
REM Without them the build stays unsigned and still works.
setlocal
cd /d "%~dp0.."

if not "%~1"=="" ( set VERSION=%~1 ) else ( set VERSION=1.0.0 )

set PYTHON=python
set VENV=.venv-build

if not exist "%VENV%\Scripts\python.exe" (
    echo ==^> Creating build venv
    %PYTHON% -m venv "%VENV%"
)
call "%VENV%\Scripts\activate.bat"

echo ==^> Installing dependencies
%PYTHON% -m pip install --quiet --upgrade pip
%PYTHON% -m pip install --quiet -r requirements.txt -r requirements-desktop.txt
%PYTHON% -m pip install --quiet pyinstaller

if not exist "build_helpers" mkdir build_helpers

if not exist "build_helpers\yt-dlp.exe" (
    echo ==^> Downloading yt-dlp for Windows
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' -OutFile 'build_helpers\yt-dlp.exe'"
)

if not exist "build_helpers\ffmpeg.exe" (
    echo ==^> Downloading static ffmpeg for Windows
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'build_helpers\ffmpeg.zip'"
    powershell -NoProfile -Command "Expand-Archive -Path 'build_helpers\ffmpeg.zip' -DestinationPath 'build_helpers\ffextract'"
    for /f "delims=" %%F in ('dir /b /s build_helpers\ffextract\ffmpeg.exe 2^>nul') do copy /y "%%F" "build_helpers\ffmpeg.exe" >nul
    rmdir /s /q build_helpers\ffextract
    del build_helpers\ffmpeg.zip
)
if not exist "build_helpers\ffmpeg.exe" (
    echo ERROR: ffmpeg.exe was not unpacked >&2
    exit /b 1
)

echo ==^> Building with PyInstaller
%PYTHON% -m PyInstaller audio-converter.spec --noconfirm --clean --distpath "%CD%\dist" --workpath "%CD%\build"

if not exist "dist\AudioConverter\AudioConverter.exe" (
    echo ERROR: bundle binary missing >&2
    exit /b 1
)

echo ==^> Smoke-testing the bundle ^(no window opened^)
set AUDIO_CONVERTER_DB_PATH=%TEMP%\audio-converter-selftest.db
set AUDIO_CONVERTER_SECRET_KEY=selftest
dist\AudioConverter\AudioConverter.exe --self-test
if errorlevel 1 (
    echo ERROR: bundle self-test failed >&2
    exit /b 1
)
del "%TEMP%\audio-converter-selftest.db*" 2>nul

where makensis >nul 2>nul
if errorlevel 1 (
    echo ==^> Installing NSIS for the Setup wizard
    choco install nsis -y
    set "PATH=C:\Program Files (x86)\NSIS;%%PATH%%"
)

echo ==^> Building the Setup wizard
makensis /DVERSION=%VERSION% scripts\installer.nsi

if not "%WINDOWS_CERT_BASE64%"=="" (
    echo ==^> Signing binaries ^(Authenticode^)
    powershell -NoProfile -Command "[IO.File]::WriteAllBytes('cert.pfx', [Convert]::FromBase64String($env:WINDOWS_CERT_BASE64))"
    where signtool >nul 2>nul
    if errorlevel 1 (
        for /f "delims=" %%S in ('dir /b /s "C:\Program Files (x86)\Windows Kits\signtool.exe" 2^>nul') do set "SIGNTOOL=%%S"
    ) else (
        for /f "delims=" %%S in ('where signtool') do set "SIGNTOOL=%%S"
    )
    if defined SIGNTOOL (
        "%SIGNTOOL%" sign /f cert.pfx /p "%WINDOWS_CERT_PASSWORD%" /tr http://timestamp.digicert.com /td sha256 /fd sha256 "dist\AudioConverter\AudioConverter.exe" "dist\AudioConverter-Setup-%VERSION%.exe"
    ) else (
        echo WARNING: signtool not found, skipping signing >&2
    )
    del cert.pfx 2>nul
) else (
    echo ==^> Skipping code signing ^(WINDOWS_CERT_BASE64 not set^)
)

echo.
echo Build complete: dist\AudioConverter-Setup-%VERSION%.exe
endlocal
