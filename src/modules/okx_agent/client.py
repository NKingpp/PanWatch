"""OKX V5 签名客户端(私有接口)。

签名规范(OKX V5 Agent Trade Kit 底层协议):
  prehash = timestamp + METHOD + requestPath(+query) + body
  sign    = Base64(HmacSHA256(prehash, secret))
请求头:
  OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP / OK-ACCESS-PASSPHRASE
  Content-Type: application/json(POST)
模拟盘: x-simulated-trading: 1

安全:密钥只从环境/参数注入;日志只打 key 前6位掩码,secret/passphrase 全掩。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://www.okx.com"
_TIMEOUT = 15.0
# 私有接口限流兜底:60 次/2s 量级,客户端节流 0.2s/请求保守值
_MIN_INTERVAL_S = 0.2
_last_request_ts = 0.0


def simulated_from_env() -> bool:
    """OKX_AGENT_SIMULATED 是否开启(env → Settings(.env) 兜底)。"""
    import os
    v = os.environ.get("OKX_AGENT_SIMULATED", "").strip().lower()
    if not v:
        try:
            from src.platform.runtime.config import Settings
            v = (Settings().okx_agent_simulated or "").strip().lower()
        except Exception:
            v = ""
    return v in ("1", "true", "yes")


class OKXAgentError(Exception):
    """结构化错误:code/msg/data 与 OKX 响应同形,可直接回传策略层。"""

    def __init__(self, code: str, msg: str, *, data: list | dict | None = None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.data = data or []

    def to_dict(self) -> dict:
        return {"code": self.code, "msg": self.msg, "data": self.data}


@dataclass
class OKXCredentials:
    api_key: str
    secret_key: str
    passphrase: str
    simulated: bool = False  # True=模拟盘
    label: str = "default"

    @classmethod
    def from_env(cls, *, simulated: bool = False, label: str = "default") -> "OKXCredentials | None":
        """从环境变量读密钥;缺任一项返回 None(不抛错,调用方决定如何提示)。

        读取顺序:os.environ → Settings(.env 文件,懒加载,只在 env 缺失时兜底)。
        """
        import os

        def _get(env_key: str, settings_field: str) -> str:
            v = os.environ.get(env_key, "").strip()
            if v:
                return v
            try:
                from src.platform.runtime.config import Settings
                return getattr(Settings(), settings_field, "").strip()
            except Exception:
                return ""

        key = _get("OKX_AGENT_API_KEY", "okx_agent_api_key")
        sec = _get("OKX_AGENT_SECRET_KEY", "okx_agent_secret_key")
        phrase = _get("OKX_AGENT_PASSPHRASE", "okx_agent_passphrase")
        if simulated:
            key = os.environ.get("OKX_AGENT_DEMO_API_KEY", key).strip()
            sec = os.environ.get("OKX_AGENT_DEMO_SECRET_KEY", sec).strip()
            phrase = os.environ.get("OKX_AGENT_DEMO_PASSPHRASE", phrase).strip()
        if not (key and sec and phrase):
            return None
        return cls(key, sec, phrase, simulated=simulated, label=label)

    @property
    def masked(self) -> str:
        """日志安全视图:只露 key 前后各 3 位。"""
        k = self.api_key
        shown = f"{k[:3]}…{k[-3:]}" if len(k) > 8 else "…"
        return f"key={shown} passphrase=*** simulated={self.simulated}"


def sign(secret: str, timestamp: str, method: str, path: str, body: str) -> str:
    """OKX V5 HMAC-SHA256 签名。path 含 query;GET body 为空串。"""
    prehash = f"{timestamp}{method}{path}{body}"
    mac = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _throttle() -> None:
    global _last_request_ts
    wait = _MIN_INTERVAL_S - (time.monotonic() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.monotonic()


def _parse_response(payload: dict, *, label: str, request_id: str) -> list | dict:
    """解析 OKX 响应体:code!=0 抛 OKXAgentError。"""
    code = str(payload.get("code", ""))
    if code != "0":
        msg = str(payload.get("msg", "未知错误"))
        logger.warning(f"[OKXAgent] {label} 业务错误 code={code} msg={msg} request_id={request_id}")
        raise OKXAgentError(code, msg, data=payload.get("data"))
    return payload.get("data") or []


def request(
    method: str,
    path: str,
    creds: OKXCredentials,
    *,
    params: dict | None = None,
    body: dict | None = None,
    proxy: str = "",
    label: str = "",
) -> list | dict:
    """带签名的 OKX 私有接口请求。同步;async 调用方用 to_thread。

    Raises:
        OKXAgentError: 业务错误码(code!=0)或网络层失败(code=-1/-2)
    """
    method = method.upper()
    query = ""
    if params:
        from urllib.parse import urlencode
        query = "?" + urlencode(params)
    full_path = f"{path}{query}"
    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""

    ts = _utc_timestamp()
    signature = sign(creds.secret_key, ts, method, full_path, body_str)

    headers = {
        "OK-ACCESS-KEY": creds.api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": creds.passphrase,
        "Content-Type": "application/json",
    }
    if creds.simulated:
        headers["x-simulated-trading"] = "1"

    _throttle()
    url = _BASE + full_path
    log_body = body_str[:200] + ("…" if len(body_str) > 200 else "") if body_str else ""
    logger.info(
        f"[OKXAgent] {label or method} {full_path} params={params or {}} "
        f"body={log_body} creds[{creds.masked}]"
    )

    try:
        with httpx.Client(timeout=_TIMEOUT, proxy=proxy or None) as client:
            if method == "GET":
                resp = client.get(url, headers=headers)
            else:
                resp = client.post(url, headers=headers, content=body_str)
    except httpx.TimeoutException as e:
        logger.error(f"[OKXAgent] {label} 超时: {e}")
        raise OKXAgentError("-1", f"请求超时: {e}") from e
    except httpx.HTTPError as e:
        logger.error(f"[OKXAgent] {label} 网络异常: {e}")
        raise OKXAgentError("-2", f"网络异常: {e}") from e

    request_id = resp.headers.get("x-Okch-Request-Id") or resp.headers.get("x-request-id") or "-"

    if resp.status_code == 429:
        logger.warning(f"[OKXAgent] {label} 限流 429 request_id={request_id}")
        raise OKXAgentError("429", "接口限流,请降低请求频率", data={"request_id": request_id})
    if resp.status_code >= 400:
        # 尝试从响应体提取 OKX 错误码;失败用 HTTP 状态码
        try:
            payload = resp.json()
            raise OKXAgentError(
                str(payload.get("code", resp.status_code)),
                f"HTTP {resp.status_code}: {payload.get('msg', '')}",
                data=payload.get("data"),
            )
        except (ValueError, KeyError):
            raise OKXAgentError(
                str(resp.status_code), f"HTTP {resp.status_code}", data=[{"request_id": request_id}]
            )

    try:
        payload = resp.json()
    except ValueError as e:
        logger.error(f"[OKXAgent] {label} 响应非 JSON: {resp.text[:200]}")
        raise OKXAgentError("-3", "响应解析失败(非 JSON)") from e

    data = _parse_response(payload, label=label, request_id=request_id)
    logger.info(f"[OKXAgent] {label} 完成 request_id={request_id} rows={len(data) if isinstance(data, list) else 1}")
    return data
