"""Tests unitaires — crypto, OTP store (fakeredis), templates, config."""

import fakeredis.aioredis
import pytest

from src.core.otp import OTPStore, verify_otp
from src.core.security import (
    compare_codes,
    generate_request_id,
    generate_secure_code,
    hash_code,
    is_in_rollout,
)
from src.core.tenants import Tenant
from src.services.email_templates import generic_html, otp_html


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="t_test",
        name="Test",
        api_key_hash="x" * 64,
        hmac_secret="secret",
        brand_name="TestBrand",
        allowed_purposes=["login_2fa"],
        otp_ttl_seconds=300,
        max_verify_attempts=3,
    )


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


# ------------------------------------------------------------------ crypto

def test_generate_code_format():
    code = generate_secure_code(6)
    assert code.isdigit() and len(code) == 6


def test_codes_are_random():
    assert generate_secure_code() != generate_secure_code()


def test_request_id_prefix():
    assert generate_request_id().startswith("req_")


def test_hash_compare():
    h = hash_code("123456")
    assert compare_codes("123456", h)
    assert not compare_codes("654321", h)


def test_rollout_bounds():
    assert is_in_rollout("u1", 100)
    assert not is_in_rollout("u1", 0)
    # déterministe
    assert is_in_rollout("u2", 50) == is_in_rollout("u2", 50)


# ------------------------------------------------------------------ OTP store

async def test_otp_lifecycle(redis, tenant):
    store = OTPStore(redis, tenant)
    code, request_id, _ = await store.create("user-uuid")

    ok, remaining = await verify_otp(redis, tenant, request_id, code)
    assert ok and remaining == 0

    # usage unique
    ok, _ = await verify_otp(redis, tenant, request_id, code)
    assert not ok


async def test_otp_wrong_code_attempts(redis, tenant):
    store = OTPStore(redis, tenant)
    code, request_id, _ = await store.create("user-uuid")

    wrong = "000000" if code != "000000" else "111111"
    ok, remaining = await verify_otp(redis, tenant, request_id, wrong)
    assert not ok and remaining == 2

    ok, remaining = await verify_otp(redis, tenant, request_id, code)
    assert ok


async def test_otp_max_attempts(redis, tenant):
    store = OTPStore(redis, tenant)
    _, request_id, _ = await store.create("user-uuid")
    wrong = "999999"

    for _ in range(tenant.max_verify_attempts):
        await verify_otp(redis, tenant, request_id, wrong)

    ok, remaining = await verify_otp(redis, tenant, request_id, wrong)
    assert not ok and remaining == 0


# ------------------------------------------------------------------ templates

def test_otp_html_contains_code_and_brand():
    html = otp_html("123456", "MonApp", "fr")
    assert "123456" in html and "MonApp" in html


def test_otp_html_escapes_brand():
    html = otp_html("123456", "<script>", "fr")
    assert "<script>" not in html


def test_generic_html_en():
    html = generic_html("Welcome", "Hello there", "Brand")
    assert "Welcome" in html and "Brand" in html
