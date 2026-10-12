@echo off
REM ============================================================
REM  OTP Email Service — lancement local en un clic (Windows)
REM  Double-cliquer sur ce fichier ou lancer : scripts\run-local.bat
REM ============================================================

cd /d "%~dp0\.."

set USE_FAKEREDIS=true
set DEBUG=true
set LOG_LEVEL=INFO
set API_KEY=test_api_key_64_chars_long_change_in_production_test_api_key_change
set HMAC_SECRET=test_hmac_secret_change_in_production
set ADMIN_API_KEY=admin_key_for_tests_64chars_minimum_required_xxxxxxxxxxxx
set EMAIL_PROVIDER=console
set FROM_EMAIL=no-reply@sendbid.example.com
set FROM_NAME=SendBID
set SMTP_HOST=smtp.gmail.com
set SMTP_PORT=587
set SMTP_USER=test@gmail.com
set SMTP_PASSWORD=test_password
set RISKY_COUNTRIES=CN,RU,BY,IR,KP
set ROLLOUT_PERCENTAGE=100
set MAX_SENDS_PER_USER_PER_10MIN=3
set MAX_SENDS_PER_IP_PER_10MIN=3
set COOLDOWN_SECONDS=60
set MAX_VERIFY_ATTEMPTS=5
set OTP_TTL_SECONDS=300

echo ============================================================
echo   OTP Email Service — mode local (fakeredis + console)
echo   Interface : http://localhost:8001/
echo   Demo      : http://localhost:8001/demo
echo   Swagger   : http://localhost:8001/docs
echo ============================================================

python -m uvicorn src.main:app --host 0.0.0.0 --port 8001
pause
