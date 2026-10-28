@echo off
setlocal
cd /d "%~dp0"
echo Iniciando NEXUS Crypto Terminal...
if not exist node_modules (
  echo Instalando dependencias web na primeira execucao...
  call npm install || goto :error
)
if not exist backend\.venv\Scripts\python.exe (
  echo Criando ambiente Python do backend...
  py -3 -m venv backend\.venv || goto :error
  call backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt || goto :error
)
start "NEXUS Market API" cmd /k "cd /d "%~dp0" && backend\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001"
timeout /t 2 /nobreak >nul
start "NEXUS Web" cmd /k "cd /d "%~dp0" && npm run dev"
timeout /t 4 /nobreak >nul
start "" http://localhost:3000
echo API: http://127.0.0.1:8001/health
echo Web: http://localhost:3000
exit /b 0
:error
echo Falha ao preparar dependencias. Verifique Node.js, npm e Python 3.
pause
exit /b 1
