@echo off
chcp 65001 >nul
cd /d "%~dp0"
set MONITOR_REDE=1
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app.py
  goto :fim
)
where python >nul 2>nul
if %errorlevel%==0 (
  python app.py
  goto :fim
)
echo Python 3 nao foi encontrado.
echo Instale em https://www.python.org/downloads/ (marque "Add python.exe to PATH") e execute este arquivo de novo.
:fim
pause
