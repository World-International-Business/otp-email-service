#!/usr/bin/env bash
# ============================================================
#  OTP Email Service — lancement local en un clic (Linux/macOS)
#  Usage : bash scripts/run-local.sh
# ============================================================

cd "$(dirname "$0")/.."

export USE_FAKEREDIS=true
export DEBUG=true
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
echo "  OTP Email Service — mode local (fakeredis + console)"
echo "  Interface : http://localhost:8001/"
echo "  Demo      : http://localhost:8001/demo"
echo "  Swagger   : http://localhost:8001/docs"
echo "============================================================"

python -m uvicorn src.main:app --host 0.0.0.0 --port 8001
