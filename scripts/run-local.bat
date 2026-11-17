@echo off
REM ============================================================
REM  OTP Email Service — lancement local en un clic (Windows)
REM  Double-cliquer sur ce fichier ou lancer : scripts\run-local.bat
REM
REM  Si un fichier .env existe a la racine, il est utilise tel quel
REM  (provider reel, credentials SMTP...). Sinon : mode demo rapide
REM  (fakeredis + emails simules dans les logs + debug_code).
REM ============================================================

cd /d "%~dp0\.."

if exist ".env" (
    REM Purger les variables heritees du shell qui ecraseraient .env
    for %%V in (USE_FAKEREDIS DEBUG DEMO_ENABLED EMAIL_PROVIDER FROM_EMAIL FROM_NAME SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD BREVO_API_KEY RESEND_API_KEY REDIS_URL PUBLIC_BASE_URL) do set "%%V="

    REM Redis persistant : demarrer le conteneur mailhub-redis si present
    docker start mailhub-redis >nul 2>&1
    echo ============================================================
    echo   OTP Email Service — config chargee depuis .env
    echo   Interface : http://localhost:8001/
    echo   Demo      : http://localhost:8001/demo ^(si DEMO_ENABLED=true^)
    echo   Swagger   : http://localhost:8001/docs
    echo ============================================================
) else (
    set USE_FAKEREDIS=true
    set DEBUG=true
    set DEMO_ENABLED=true
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
    echo   OTP Email Service — mode demo rapide ^(pas de .env^)
    echo   Emails SIMULES dans les logs + code affiche sur /demo.
    echo   Pour de vrais emails : copier .env.example en .env
    echo   et renseigner SMTP_USER / SMTP_PASSWORD.
    echo   Interface : http://localhost:8001/
    echo   Demo      : http://localhost:8001/demo
    echo   Swagger   : http://localhost:8001/docs
    echo ============================================================
)

REM Utiliser le Python du venv local s'il existe, sinon le Python global
set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

%PY% -m uvicorn src.main:app --host 0.0.0.0 --port 8001
pause
