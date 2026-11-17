#!/usr/bin/env bash
# ============================================================
#  OTP Email Service — lancement local en un clic (Linux/macOS)
#  Usage : bash scripts/run-local.sh
#
#  Si un fichier .env existe à la racine, il est utilisé tel quel
#  (provider réel, credentials SMTP...). Sinon : mode démo rapide
#  (fakeredis + emails simulés dans les logs + debug_code).
# ============================================================

cd "$(dirname "$0")/.."

if [ -f .env ]; then
    # Purger les variables héritées du shell qui écraseraient .env
    unset USE_FAKEREDIS DEBUG DEMO_ENABLED EMAIL_PROVIDER FROM_EMAIL FROM_NAME \
          SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD BREVO_API_KEY \
          RESEND_API_KEY REDIS_URL PUBLIC_BASE_URL
    echo "============================================================"
    echo "  OTP Email Service — config chargée depuis .env"
    echo "  Interface : http://localhost:8001/"
    echo "  Demo      : http://localhost:8001/demo (si DEMO_ENABLED=true)"
    echo "  Swagger   : http://localhost:8001/docs"
    echo "============================================================"
else
    export USE_FAKEREDIS=true
    export DEBUG=true
    export DEMO_ENABLED=true
    export LOG_LEVEL=INFO
    export API_KEY=test_api_key_64_chars_long_change_in_production_test_api_key_change
    export HMAC_SECRET=test_hmac_secret_change_in_production
    export ADMIN_API_KEY=admin_key_for_tests_64chars_minimum_required_xxxxxxxxxxxx
    export EMAIL_PROVIDER=console
    export FROM_EMAIL=no-reply@sendbid.example.com
    export FROM_NAME=SendBID
    export SMTP_HOST=smtp.gmail.com
    export SMTP_PORT=587
    export SMTP_USER=test@gmail.com
    export SMTP_PASSWORD=test_password
    export RISKY_COUNTRIES=CN,RU,BY,IR,KP
    export ROLLOUT_PERCENTAGE=100
    export MAX_SENDS_PER_USER_PER_10MIN=3
    export MAX_SENDS_PER_IP_PER_10MIN=3
    export COOLDOWN_SECONDS=60
    export MAX_VERIFY_ATTEMPTS=5
    export OTP_TTL_SECONDS=300

    echo "============================================================"
    echo "  OTP Email Service — mode démo rapide (pas de .env)"
    echo "  Emails SIMULÉS dans les logs + code affiché sur /demo."
    echo "  Pour de vrais emails : copier .env.example en .env"
    echo "  et renseigner SMTP_USER / SMTP_PASSWORD."
    echo "  Interface : http://localhost:8001/"
    echo "  Demo      : http://localhost:8001/demo"
    echo "  Swagger   : http://localhost:8001/docs"
    echo "============================================================"
fi

# Utiliser le Python du venv local s'il existe, sinon le Python global
PY=python
if [ -x venv/bin/python ]; then PY=venv/bin/python; fi

$PY -m uvicorn src.main:app --host 0.0.0.0 --port 8001
