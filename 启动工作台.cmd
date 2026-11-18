@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "artifacts\workbench\report.html" goto serve
python -X utf8 -B examples/invoice_demo.py
if errorlevel 1 goto finish
python -X utf8 -B -m traceguard evaluate --cases artifacts/demo/invoice.jsonl --output artifacts/workbench
if errorlevel 1 goto finish
:serve
python -X utf8 -B -m traceguard serve --port 8765 --output artifacts/workbench
:finish
pause
