@echo off
setlocal
set "arch=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "arch=%PROCESSOR_ARCHITEW6432%"
if /i "%arch%"=="AMD64" goto x64
if /i "%arch%"=="ARM64" goto arm64
echo This client requires x64 or ARM64 Windows.
pause
exit /b 2
:x64
"%~dp0windows\CloudLinuxWorkspace-x64.exe" %*
exit /b %errorlevel%
:arm64
"%~dp0windows\CloudLinuxWorkspace-arm64.exe" %*
exit /b %errorlevel%
