@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" goto native
if /i "%~1"=="--native" goto native
if /i "%~1"=="--distro" goto native
if /i "%~1"=="--check-native" goto native
if /i "%~1"=="--setup-native" goto native
if /i "%~1"=="--help" goto native
if /i "%~1"=="--open-app" goto native
where py >nul 2>nul
if errorlevel 1 (
  python launcher.py %*
) else (
  py -3 launcher.py %*
)
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%

:native
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
start "" "%~dp0windows\CloudLinuxWorkspace-%target%.exe" %*
exit /b %errorlevel%
