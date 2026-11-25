@echo off
chcp 65001 >nul
title Monitor de Processos - nao feche esta janela
cd /d "%~dp0"
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python 3.9 ou mais novo nao foi encontrado neste computador.
  echo.
  echo Instale em https://www.python.org/downloads/ e, na primeira tela do instalador,
  echo MARQUE a caixa "Add python.exe to PATH". Depois execute este arquivo de novo.
  echo.
  pause
  exit /b 1
)
%PY% app.py
echo.
echo O Monitor foi encerrado. Se apareceu algum erro acima, envie uma foto desta janela.
pause
