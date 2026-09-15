@echo off
REM Build the AudioConverter desktop app for Windows.
REM   -> dist\AudioConverter\AudioConverter.exe
setlocal
cd /d "%~dp0.."

set PYTHON=python
set VENV=.venv-desktop

echo ==^> Creating build venv at %VENV%
%PYTHON% -m venv %VENV%
call %VENV%\Scripts\activate.bat

echo ==^> Installing app dependencies
%PYTHON% -m pip install --quiet --upgrade pip
%PYTHON% -m pip install --quiet -r requirements.txt -r requirements-desktop.txt
%PYTHON% -m pip install --quiet pyinstaller

echo ==^> Building with PyInstaller
%PYTHON% -m PyInstaller --noconfirm --clean desktop.spec

echo.
echo Build complete. Artifact:
dir /b dist\AudioConverter
endlocal