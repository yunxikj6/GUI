@echo off
call "%~dp0Start.cmd" --setup-native %*
exit /b %errorlevel%
