@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

REM magic 一键启动/停止（Windows）
REM   双击 magic.cmd  或  magic.bat
REM   后台运行：magic.bat bg
REM   仅停止：magic.bat stop

cd /d "%~dp0"
set "ROOT=%CD%"
set "PROXY_PORT=8787"
set "WEB_PORT=8765"
set "APP_URL=http://127.0.0.1:%WEB_PORT%/index.html"
set "PID_FILE=%ROOT%\.proxy.pid"
set "WEB_PID_FILE=%ROOT%\.web.pid"
set "LOG_FILE=%ROOT%\.proxy.log"
set "WEB_LOG=%ROOT%\.web.log"

call :find_python
if errorlevel 1 (
  echo [X] 未找到 Python，请先安装 Python 3 并勾选 "Add Python to PATH"
  pause
  exit /b 1
)

set "CMD=%~1"
if "%CMD%"=="" set "CMD=start"

if /i "%CMD%"=="stop" goto :do_stop
if /i "%CMD%"=="bg" goto :do_bg
if /i "%CMD%"=="start" goto :do_start

echo 用法: %~nx0 [start^|bg^|stop]  （默认 start）
exit /b 1

:do_stop
call :stop_services
goto :eof

:do_bg
call :start_proxy
if errorlevel 1 exit /b 1
call :start_web
call :print_phone_hint
echo.
echo [OK] 已在后台运行；停止请执行: magic.bat stop
goto :eof

:do_start
call :start_proxy
if errorlevel 1 (
  pause
  exit /b 1
)
call :start_web
start "" "%APP_URL%"
call :print_phone_hint
echo.
echo [OK] 电脑已打开（估值应显示 ·蛋卷，否则点刷新）
echo [!] 按任意键会停止服务，手机也将无法访问
echo.
pause
call :stop_services
goto :eof

:find_python
REM 优先 python，其次 Windows 启动器 py，再次 python3
set "PY="
where python >nul 2>&1 && (
  REM 排除 Windows Store 假 python（只是打开商店的 stub）
  for /f "delims=" %%i in ('where python') do (
    echo %%i | findstr /I "WindowsApps" >nul || (
      set "PY=%%i"
      exit /b 0
    )
  )
)
where py >nul 2>&1 && set "PY=py" && exit /b 0
where python3 >nul 2>&1 && set "PY=python3" && exit /b 0
exit /b 1

:port_in_use
netstat -ano | findstr /R /C:":%~1 .*LISTENING" >nul 2>&1
exit /b %errorlevel%

:kill_port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":%~1 .*LISTENING"') do (
  if not "%%a"=="0" taskkill /F /PID %%a >nul 2>&1
)
exit /b 0

:stop_services
if exist "%PID_FILE%" (
  set /p PROXY_PID=<"%PID_FILE%"
  if defined PROXY_PID taskkill /F /PID !PROXY_PID! >nul 2>&1
  del /f /q "%PID_FILE%" >nul 2>&1
)
call :kill_port %PROXY_PORT%

if exist "%WEB_PID_FILE%" (
  set /p WEB_PID=<"%WEB_PID_FILE%"
  if defined WEB_PID taskkill /F /PID !WEB_PID! >nul 2>&1
  del /f /q "%WEB_PID_FILE%" >nul 2>&1
)
call :kill_port %WEB_PORT%

echo [OK] 蛋卷代理与本地网页已停止
exit /b 0

:start_proxy
call :port_in_use %PROXY_PORT%
if not errorlevel 1 (
  echo [OK] 蛋卷代理已在运行 (:%PROXY_PORT%)
  exit /b 0
)
echo [-] 启动蛋卷代理...
if /i "%PY%"=="py" (
  start /b "" cmd /c "py -3 proxy\valuation.py >>"%LOG_FILE%" 2>&1"
) else (
  start /b "" cmd /c ""%PY%" proxy\valuation.py >>"%LOG_FILE%" 2>&1"
)

set "PROXY_READY="
for /l %%i in (1,1,20) do (
  powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:%PROXY_PORT%/' -UseBasicParsing -TimeoutSec 1).StatusCode | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    set "PROXY_READY=1"
    goto :proxy_started
  )
  timeout /t 1 /nobreak >nul
)

:proxy_started
if not defined PROXY_READY (
  echo [X] 代理启动失败，查看 %LOG_FILE%
  exit /b 1
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":%PROXY_PORT% .*LISTENING"') do (
  >"%PID_FILE%" echo %%a
  goto :proxy_pid_done
)
:proxy_pid_done
echo [OK] 蛋卷代理 http://127.0.0.1:%PROXY_PORT%
exit /b 0

:start_web
call :port_in_use %WEB_PORT%
if not errorlevel 1 (
  echo [OK] 本地网页已在运行 (:%WEB_PORT%)
  exit /b 0
)
echo [-] 启动本地网页（局域网可访问）...
if /i "%PY%"=="py" (
  start /b "" cmd /c "py -3 -m http.server %WEB_PORT% --bind 0.0.0.0 >>"%WEB_LOG%" 2>&1"
) else (
  start /b "" cmd /c ""%PY%" -m http.server %WEB_PORT% --bind 0.0.0.0 >>"%WEB_LOG%" 2>&1"
)
timeout /t 1 /nobreak >nul

for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":%WEB_PORT% .*LISTENING"') do (
  >"%WEB_PID_FILE%" echo %%a
  goto :web_pid_done
)
:web_pid_done
echo [OK] 电脑访问 %APP_URL%
exit /b 0

:print_phone_hint
set "LAN_IP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do (
  set "IP=%%a"
  set "IP=!IP: =!"
  echo !IP! | findstr /R "^192\.168\." >nul && set "LAN_IP=!IP!" && goto :got_ip
  echo !IP! | findstr /R "^10\." >nul && if not defined LAN_IP set "LAN_IP=!IP!"
  echo !IP! | findstr /R "^172\.(1[6-9]|2[0-9]|3[0-1])\." >nul && if not defined LAN_IP set "LAN_IP=!IP!"
)
:got_ip
if defined LAN_IP (
  echo [手机] 同一 WiFi 打开: http://!LAN_IP!:%WEB_PORT%/index.html
  echo        可添加到主屏幕，当 App 用
) else (
  echo [手机] 设置 - 网络 查看本机 IP，浏览器打开 http://IP:%WEB_PORT%/index.html
)
exit /b 0
