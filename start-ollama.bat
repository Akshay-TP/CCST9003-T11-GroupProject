@echo off
setlocal
cd /d "%~dp0"
where ollama >nul 2>&1
if errorlevel 1 (
    echo Install Ollama from https://ollama.com/download/windows then run this file again.
    pause
    exit /b 1
)
ollama pull llama3.2:3b
if errorlevel 1 (
    echo Open the Ollama application and try again.
    pause
    exit /b 1
)
python app.py --ollama-model llama3.2:3b
pause
