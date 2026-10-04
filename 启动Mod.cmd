@echo off
where python.exe >nul 2>nul
if errorlevel 1 goto use_py
python.exe -B -X utf8 "%~dp0mod.py" serve
goto done
:use_py
py.exe -3 -B -X utf8 "%~dp0mod.py" serve
:done
pause
