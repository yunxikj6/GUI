@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" goto windows_launcher
if /i "%~1"=="--connect-port" goto windows_launcher
if /i "%~1"=="--instabox" goto windows_launcher
if /i "%~1"=="--machine" goto windows_launcher
if /i "%~1"=="--desktop-port" goto windows_launcher
if /i "%~1"=="--desktop-size" goto windows_launcher
if /i "%~1"=="--desktop-forwarded-port" goto windows_launcher
if /i "%~1"=="--cloud-app" goto windows_launcher
if /i "%~1"=="--native" goto windows_launcher
if /i "%~1"=="--distro" goto windows_launcher
if /i "%~1"=="--check-native" goto windows_launcher
if /i "%~1"=="--setup-native" goto windows_launcher
if /i "%~1"=="--help" goto windows_launcher
if /i "%~1"=="--open-app" goto windows_launcher
if not exist "%~dp0launcher.py" goto windows_launcher
where py >nul 2>nul
if errorlevel 1 (
  python launcher.py %*
) else (
  py -3 launcher.py %*
)
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%

:windows_launcher
set "target="
set "arch=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "arch=%PROCESSOR_ARCHITEW6432%"
if /i "%arch%"=="AMD64" set "target=x64"
if /i "%arch%"=="ARM64" set "target=arm64"
if not defined target (
  echo This launcher requires x64 or ARM64 Windows.
  pause
  exit /b 2
)
if /i "%~1"=="--open-app" goto open_app
"%~dp0windows\CloudLinuxWorkspace-%target%.exe" %*
exit /b %errorlevel%

:open_app
"%~dp0windows\CloudLinuxWorkspace-%target%.exe" %* --offer-install
exit /b %errorlevel%
