@echo off
chcp 65001 >nul
REM Double-click to start magic on Windows
cd /d "%~dp0"
if not exist "%~dp0magic.bat" (
  echo [X] 找不到 magic.bat，请与 magic.cmd 放在同一目录
  pause
  exit /b 1
)
call "%~dp0magic.bat" %*
if errorlevel 1 pause
