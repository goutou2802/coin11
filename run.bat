@echo off
setlocal
chcp 65001 >nul
title coin11 -- one command: panel + one round
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo [1/2] starting panel on http://127.0.0.1:8899 ...
echo        (the panel opens its own window: browser --app mode, no tabs)
start "coin11-dash" /min python "coin11\dash.py"

echo [2/2] asking the panel to run ONE round (coin only, 30 min) ...
powershell -NoProfile -Command "$b='{\"whats\":[\"coin\"],\"minutes\":30}'; $ok=$false; for($i=0;$i -lt 12;$i++){ try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8899/api/start' -Method Post -ContentType 'application/json' -Body $b -TimeoutSec 20; 'round started: pid ' + $r.pid + ' on ' + $r.label; $ok=$true; break } catch { Start-Sleep -Milliseconds 800 } }; if(-not $ok){ 'NOT started -- open http://127.0.0.1:8899 and click the start button' }"

echo.
echo This window closes by itself. Watch/stop/shutdown on the panel window.
ping -n 3 127.0.0.1 >nul
