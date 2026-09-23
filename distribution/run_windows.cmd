@echo off
cd /d "%~dp0"
py -3.12 --version >nul 2>nul
if errorlevel 1 (
  echo Python 3.12 명령(py -3.12)을 설치한 뒤 다시 실행하세요.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe py -3.12 -m venv .venv
if errorlevel 1 (pause & exit /b 1)
.venv\Scripts\python.exe -m pip install -e ".[cad,results,media]"
if errorlevel 1 (pause & exit /b 1)
echo 웹앱: http://127.0.0.1:8765/
echo 이 창을 열어 둔 채 브라우저에서 위 주소로 접속하세요.
.venv\Scripts\python.exe -m cfd_twin serve-workflow --case-dir data\case --cad-dir data\cad --viewer-dir viewer --port 8765
pause
