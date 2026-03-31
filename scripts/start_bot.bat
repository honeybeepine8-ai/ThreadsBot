@echo off
cd /d C:\ThreadsBot
C:\ThreadsBot\.venv\Scripts\pythonw.exe -m core.scheduler all >> C:\ThreadsBot\data\logs\startup.log 2>&1
