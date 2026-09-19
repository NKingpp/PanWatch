"""OKX Agent 模块单测:签名/校验/错误解析/脱敏/开关,不发真实请求。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from src.modules.okx_agent.client import (
    OKXAgentError,
    OKXCredentials,
    _parse_response,
    sign,
)
from src.modules.okx_agent.service import AgentOrderRequest, OKXAgentService


# ---------- 签名 ----------

def test_sign_matches_okx_formula():
    """sign = Base64(HmacSHA256(ts+method+path+body, secret))"""
    ts, secret = "2026-09-19T08:00:00.000Z", "mysecret"
    expected = base64.b64encode(
        hmac.new(
            secret.encode(),
            f"{ts}GET/api/v5/account/balance".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert sign(secret, ts, "GET", "/api/v5/account/balance", "") == expected


def test_sign_post_with_body_and_query():
    ts, secret = "2026-09-19T08:00:00.000Z", "s3cr3t"
    body = json.dumps({"instId": "BTC-USDT", "side": "buy"}, separators=(",", ":"))
    prehash = f"{ts}POST/api/v5/trade/order?foo=1{body}"
    expected = base64.b64encode(
        hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()
    assert sign(secret, ts, "POST", "/api/v5/trade/order?foo=1", body) == expected


# ---------- 密钥 ----------

def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("OKX_AGENT_API_KEY", "abc123key")
    monkeypatch.setenv("OKX_AGENT_SECRET_KEY", "secret!")
    monkeypatch.setenv("OKX_AGENT_PASSPHRASE", "phrase!")
    c = OKXCredentials.from_env()
    assert c is not None and c.api_key == "abc123key" and not c.simulated


def test_credentials_missing_returns_none(monkeypatch):
    for k in ("OKX_AGENT_API_KEY", "OKX_AGENT_SECRET_KEY", "OKX_AGENT_PASSPHRASE"):
        monkeypatch.delenv(k, raising=False)
    assert OKXCredentials.from_env() is None


def test_credentials_masked_never_leaks_secret():
    c = OKXCredentials("abcdefghijklmnop", "topsecret", "phrase123456")
    masked = c.masked
    assert "topsecret" not in masked
    assert "phrase123456" not in masked
    assert "abcdefghijklmnop" not in masked
    assert "abc" in masked  # 前 3 位可见


def test_credentials_demo_env_priority(monkeypatch):
    monkeypatch.setenv("OKX_AGENT_API_KEY", "livekey")
    monkeypatch.setenv("OKX_AGENT_SECRET_KEY", "livesecret")
    monkeypatch.setenv("OKX_AGENT_PASSPHRASE", "livephrase")
    monkeypatch.setenv("OKX_AGENT_DEMO_API_KEY", "demokey")
    monkeypatch.setenv("OKX_AGENT_DEMO_SECRET_KEY", "demosecret")
    monkeypatch.setenv("OKX_AGENT_DEMO_PASSPHRASE", "demophrase")
    c = OKXCredentials.from_env(simulated=True)
    assert c.api_key == "demokey" and c.simulated


# ---------- 响应解析 ----------

def test_parse_response_success():
    assert _parse_response({"code": "0", "data": [{"a": 1}]}, label="t", request_id="r") == [{"a": 1}]


def test_parse_response_biz_error():
    with pytest.raises(OKXAgentError) as ei:
        _parse_response({"code": "51000", "msg": "Parameter error"}, label="t", request_id="r")
    assert ei.value.code == "51000" and "Parameter" in ei.value.msg


def test_error_to_dict():
    e = OKXAgentError("429", "rate limited", data=[{"x": 1}])
    d = e.to_dict()
    assert d == {"code": "429", "msg": "rate limited", "data": [{"x": 1}]}


# ---------- 下单参数校验 ----------

def _req(**kw):
    base = dict(inst_id="BTC-USDT", side="buy", ord_type="market", sz="0.01", td_mode="cash")
    base.update(kw)
    return AgentOrderRequest(**base)


def test_order_request_valid():
    r = _req()
    r.validate()
    assert r.to_okx_body() == {
        "instId": "BTC-USDT", "tdMode": "cash", "side": "buy", "ordType": "market", "sz": "0.01",
    }


def test_order_request_limit_needs_px():
    with pytest.raises(OKXAgentError) as ei:
        _req(ord_type="limit").validate()
    assert "px" in ei.value.msg


def test_order_request_bad_side():
    with pytest.raises(OKXAgentError):
        _req(side="hold").validate()


def test_order_request_bad_sz():
    with pytest.raises(OKXAgentError):
        _req(sz="0").validate()
    with pytest.raises(OKXAgentError):
        _req(sz="abc").validate()


def test_order_request_bad_instid():
    with pytest.raises(OKXAgentError):
        _req(inst_id="AAPL").validate()


def test_order_request_cl_ord_id_and_reduce_only():
    r = _req(cl_ord_id="pw-001", reduce_only=True, ord_type="limit", px="50000")
    r.validate()
    body = r.to_okx_body()
    assert body["clOrdId"] == "pw-001" and body["reduceOnly"] is True and body["px"] == "50000"


# ---------- 开关 ----------

def test_disabled_service_rejects_order():
    svc = OKXAgentService(OKXCredentials("k", "s", "p"), enabled=False)
    with pytest.raises(OKXAgentError) as ei:
        svc.place_order(_req(), db=None, account_id=0)
    assert ei.value.code == "AGENT_DISABLED"


def test_disabled_service_rejects_cancel():
    svc = OKXAgentService(OKXCredentials("k", "s", "p"), enabled=False)
    with pytest.raises(OKXAgentError) as ei:
        svc.cancel_order("BTC-USDT", ord_id="123")
    assert ei.value.code == "AGENT_DISABLED"
