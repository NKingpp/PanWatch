"""OKX Agent 交易服务层:参数校验、订单生命周期、状态查询。

业务约定:
- 所有下单/撤单先写 DB(status=submitted),拿到回执更新 okx 响应,失败置 failed;
- enable 开关关闭时拒绝一切写操作(读接口仍可用);
- 多账户:账号记录存 DB,密钥不落库(仅 simulated/label),私钥从 env 读。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from src.modules.okx_agent.client import OKXAgentError, OKXCredentials, request

logger = logging.getLogger(__name__)


# ---------- 请求结构体(参数校验) ----------

_SIDE = {"buy", "sell"}
_TD_MODE = {"cash", "cross", "isolated"}
_ORD_TYPE = {"market", "limit", "post_only", "fok", "ioc", "optimal_limit_IOC"}


@dataclass
class AgentOrderRequest:
    """下单请求;字段与 OKX /api/v5/trade/order 对齐,构造时校验。"""

    inst_id: str
    side: str                     # buy / sell
    ord_type: str = "market"      # market / limit / ...
    sz: str = ""                  # 数量(字符串,保留精度)
    px: str = ""                  # 限价单价格
    td_mode: str = "cash"         # cash(现货) / cross / isolated
    cl_ord_id: str = ""           # 客户端订单号(幂等键)
    reduce_only: bool | str = ""  # 合约减仓

    def validate(self) -> None:
        errors = []
        if not self.inst_id or "-" not in self.inst_id:
            errors.append("inst_id 必须是 OKX 交易对(如 BTC-USDT)")
        if self.side not in _SIDE:
            errors.append(f"side 必须是 {'/'.join(sorted(_SIDE))}")
        if self.ord_type not in _ORD_TYPE:
            errors.append(f"ord_type 非法: {self.ord_type}")
        if self.td_mode not in _TD_MODE:
            errors.append(f"td_mode 非法: {self.td_mode}")
        try:
            if float(self.sz) <= 0:
                errors.append("sz 必须大于 0")
        except (TypeError, ValueError):
            errors.append("sz 必须是正数字")
        if self.ord_type in ("limit", "post_only", "fok", "ioc"):
            try:
                if float(self.px or 0) <= 0:
                    errors.append(f"{self.ord_type} 单必须给 px")
            except (TypeError, ValueError):
                errors.append("px 必须是正数字")
        if errors:
            raise OKXAgentError("INVALID_PARAM", "; ".join(errors))

    def to_okx_body(self) -> dict:
        body = {
            "instId": self.inst_id,
            "tdMode": self.td_mode,
            "side": self.side,
            "ordType": self.ord_type,
            "sz": self.sz,
        }
        if self.px:
            body["px"] = self.px
        if self.cl_ord_id:
            body["clOrdId"] = self.cl_ord_id
        if self.reduce_only:
            body["reduceOnly"] = self.reduce_only
        return body


@dataclass
class OrderExecutionRecord:
    """一次 Agent 执行的落库快照。"""
    cl_ord_id: str = ""
    ord_id: str = ""
    status: str = "submitted"    # submitted / live / partially_filled / filled / canceled / failed
    error_code: str = ""
    error_msg: str = ""
    okx_response: dict = field(default_factory=dict)


# ---------- 服务 ----------

class OKXAgentService:
    """Agent 交易服务。每个实例绑定一个账户配置(多账户=多实例)。"""

    def __init__(self, creds: OKXCredentials, *, enabled: bool = True, proxy: str = ""):
        self.creds = creds
        self.enabled = enabled
        self.proxy = proxy

    # ---- 写接口(交易) ----

    def place_order(self, req: AgentOrderRequest, db: Session, account_id: int) -> OrderExecutionRecord:
        """Agent 下单。开关关闭直接拒绝。"""
        if not self.enabled:
            raise OKXAgentError("AGENT_DISABLED", "Agent 自动交易开关已关闭,拒绝下单")
        req.validate()

        rec = OrderExecutionRecord(cl_ord_id=req.cl_ord_id)
        # 先落 submitted
        row = _insert_order_row(db, account_id, req, rec)

        try:
            data = request(
                "POST", "/api/v5/trade/order", self.creds,
                body=req.to_okx_body(), proxy=self.proxy, label="place_order",
            )
            item = data[0] if isinstance(data, list) and data else {}
            rec.ord_id = str(item.get("ordId", ""))
            rec.cl_ord_id = str(item.get("clOrdId", rec.cl_ord_id))
            rec.status = "live" if item.get("sCode") == "0" else "failed"
            rec.okx_response = item
            if item.get("sCode") != "0":
                rec.error_code = str(item.get("sCode", ""))
                rec.error_msg = str(item.get("sMsg", ""))
        except OKXAgentError as e:
            rec.status = "failed"
            rec.error_code = e.code
            rec.error_msg = e.msg
            rec.okx_response = e.to_dict()

        _update_order_row(db, row, rec)
        if rec.status == "failed":
            raise OKXAgentError(rec.error_code or "ORDER_FAILED", rec.error_msg or "下单失败", data=rec.okx_response)
        return rec

    def cancel_order(self, inst_id: str, ord_id: str = "", cl_ord_id: str = "", *, db: Session = None, account_id: int = 0) -> dict:
        """Agent 撤单。"""
        if not self.enabled:
            raise OKXAgentError("AGENT_DISABLED", "Agent 自动交易开关已关闭,拒绝撤单")
        if not (ord_id or cl_ord_id):
            raise OKXAgentError("INVALID_PARAM", "ord_id 与 cl_ord_id 至少给一个")
        body: dict = {"instId": inst_id}
        if ord_id:
            body["ordId"] = ord_id
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        data = request(
            "POST", "/api/v5/trade/cancel-order", self.creds,
            body=body, proxy=self.proxy, label="cancel_order",
        )
        return data[0] if isinstance(data, list) and data else {}

    # ---- 读接口(状态查询) ----

    def order_status(self, *, inst_id: str = "", cl_ord_id: str = "", ord_id: str = "") -> list[dict]:
        """查询订单执行状态(供策略回调判断)。"""
        params: dict = {}
        if inst_id:
            params["instId"] = inst_id
        if ord_id:
            params["ordId"] = ord_id
        if cl_ord_id:
            params["clOrdId"] = cl_ord_id
        if not params:
            raise OKXAgentError("INVALID_PARAM", "至少提供一个查询条件")
        return request("GET", "/api/v5/trade/order", self.creds, params=params, proxy=self.proxy, label="order_status")

    def pending_orders(self, inst_type: str = "", inst_id: str = "") -> list[dict]:
        """当前挂单。"""
        params = {k: v for k, v in {"instType": inst_type, "instId": inst_id}.items() if v}
        return request("GET", "/api/v5/trade/orders-pending", self.creds, params=params, proxy=self.proxy, label="pending_orders")

    def account_balance(self, ccy: str = "") -> list[dict]:
        """账户余额(只读)。"""
        params = {"ccy": ccy} if ccy else None
        return request("GET", "/api/v5/account/balance", self.creds, params=params, proxy=self.proxy, label="balance")

    def positions(self, inst_type: str = "", inst_id: str = "") -> list[dict]:
        """当前持仓(只读)。"""
        params = {k: v for k, v in {"instType": inst_type, "instId": inst_id}.items() if v}
        return request("GET", "/api/v5/account/positions", self.creds, params=params, proxy=self.proxy, label="positions")


# ---------- DB 记录(轻量,直接用 text SQL 避免绑死 ORM 模型) ----------

def _insert_order_row(db: Session, account_id: int, req: AgentOrderRequest, rec: OrderExecutionRecord):
    from sqlalchemy import text
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        text("""
INSERT INTO okx_agent_orders
    (account_id, inst_id, side, ord_type, td_mode, sz, px, cl_ord_id, status, created_at, updated_at)
VALUES (:aid, :iid, :side, :ot, :td, :sz, :px, :cid, :st, :now, :now)
"""),
        {
            "aid": account_id, "iid": req.inst_id, "side": req.side, "ot": req.ord_type,
            "td": req.td_mode, "sz": req.sz, "px": req.px, "cid": req.cl_ord_id or "",
            "st": rec.status, "now": now,
        },
    )
    db.commit()
    return row


def _update_order_row(db: Session, row, rec: OrderExecutionRecord) -> None:
    from sqlalchemy import text
    import json as _json
    db.execute(
        text("""
UPDATE okx_agent_orders
SET ord_id = :oid, status = :st, error_code = :ec, error_msg = :em,
    okx_response = :resp, updated_at = :now
WHERE cl_ord_id = :cid AND ord_id IS NULL
"""),
        {
            "oid": rec.ord_id, "st": rec.status, "ec": rec.error_code, "em": rec.error_msg,
            "resp": _json.dumps(rec.okx_response, ensure_ascii=False)[:4000],
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cid": rec.cl_ord_id or rec.ord_id or "-",
        },
    )
    db.commit()


# OKX sCode → 内部状态
def map_okx_state(state: str) -> str:
    return {
        "live": "live", "partially_filled": "partially_filled", "filled": "filled",
        "canceled": "canceled",
    }.get(state, state or "unknown")
