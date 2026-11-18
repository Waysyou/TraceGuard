@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -X utf8 -B examples/invoice_demo.py
if errorlevel 1 goto finish
python -X utf8 -B -m traceguard evaluate --cases artifacts/demo/invoice.jsonl --output artifacts/workbench
:finish
pause
