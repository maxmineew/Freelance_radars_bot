@echo off
rem Scheduled run of the lead bot; output is appended to run.log
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ==== %date% %time% >> run.log
python -u run.py >> run.log 2>&1
