@echo off
where python.exe >nul 2>nul
if errorlevel 1 goto use_py
python.exe -B -X utf8 "%~dp0extract_prompts.py" %*
goto done
:use_py
py.exe -3 -B -X utf8 "%~dp0extract_prompts.py" %*
:done
pause
