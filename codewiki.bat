@echo off
set PYTHONPATH=%~dp0src
python -m codewiki.cli %*
