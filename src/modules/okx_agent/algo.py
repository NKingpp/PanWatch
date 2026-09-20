"""策略委托下单层:OKX V5 Algo Trading 接口封装。

接口(https://www.okx.com/docs-v5/zh/#order-book-trading-algo-trading):
- POST /api/v5/trade/order-algo          下策略委托单
- POST /api/v5/trade/cancel-algos        撤销策略委托(批量)
- GET  /api/v5/trade/orders-algo-pending 未触发策略委托列表
- GET  /api/v5/trade/orders-algo-history 策略委托历史(ordType=triggered 查已触发)

支持 ordType:conditional(止盈止损) / oco(现货+合约) / trigger(计划委托) / move(移动止盈) / chord(冰山+时间加权)

分层:本层只负责「构造参数 → 签名请求 → 落库快照」,风控在 risk.py,确认流在 proposal.py。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.modules.okx_agent.client import OKXAgentError, OKXCredentials, request

logger = logging.getLogger(__name__)

_ALGO_ORD_TYPES = {
    "conditional",  # 止盈止损(合约/现货)
    "oco",          # OCO(现货/合约:触发+委托)
    "trigger",      # 计划委托
    "move",         # 移动止盈(仅限价)
    "chord",        # 冰山/时间加权(高级策略)
}


@dataclass
class AlgoOrderRequest:
    """策略委托请求;字段按 ordType 动态必填,validate() 校验。"""

    inst_id: str
    td_mode: str                     # cash / cross / isolated
    side: str                        # buy / sell
    ord_type: str                    # conditional / oco / trigger / move / chord
    sz: str = ""                     # 数量(SPOT=币;合约=张)
    # conditional / trigger / oco:触发价
    trigger_px: str = ""
    trigger_px_type: str = "last"    # last / index / mark
    # 委托价(空=市价)
    order_px: str = ""
    # conditional(止盈止损):止盈价/止损价(至少一个)+ 拦截触发价
    tp_trigger_px: str = ""
    tp_ord_px: str = "-1"            # -1 表示市价触发委托
    sl_trigger_px: str = ""
    sl_ord_px: str = "-1"
    # move(移动止盈)
    callback_ratio: str = ""
    move_trigger_px: str = ""
    # 通用
    reduce_only: bool = False
    cl_ord_id: str = ""              # 幂等键
    pos_mode: str = ""               # long_short=双向(需 posSide);net/空=单向(禁传)
    # chord(冰山/TWAP)未开放给提案层,预留
    tag: str = "panwatch_algo"

    def validate(self) -> None:
        errors: list[str] = []
        if not self.inst_id or "-" not in self.inst_id:
            errors.append("inst_id 必须是 OKX 交易对")
        if self.side not in ("buy", "sell"):
            errors.append("side 必须是 buy/sell")
        if self.td_mode not in ("cash", "cross", "isolated"):
            errors.append("td_mode 非法")
        if self.ord_type not in _ALGO_ORD_TYPES:
            errors.append(f"ord_type 必须是 {'/'.join(sorted(_ALGO_ORD_TYPES))}")
        try:
            if float(self.sz) <= 0:
                errors.append("sz 必须大于 0")
        except (TypeError, ValueError):
            errors.append("sz 必须是正数")
        # 按类型必填项
        need_px: list[tuple[str, str]] = []
        if self.ord_type == "trigger":
            need_px = [("trigger_px", "触发价")]
        elif self.ord_type == "conditional":
            # 止盈止损:tp/sl 触发价至少一个
            if not (self.tp_trigger_px or self.sl_trigger_px):
                errors.append("conditional 需要止盈或止损触发价(至少一个)")
        elif self.ord_type == "oco":
            if not (self.tp_trigger_px or self.sl_trigger_px):
                errors.append("oco 需要止盈或止损触发价(至少一个)")
        elif self.ord_type == "move":
            need_px = [("callback_ratio", "回调幅度"), ("move_trigger_px", "移动触发价")]
        for attr, name in need_px:
            v = getattr(self, attr, "")
            try:
                if float(v or 0) <= 0:
                    errors.append(f"{name}必须大于 0")
            except (TypeError, ValueError):
                errors.append(f"{name}非法")
        if errors:
            raise OKXAgentError("INVALID_PARAM", "; ".join(errors))

    def to_okx_body(self) -> dict:
        """构造 OKX order-algo 请求体(按 ordType 裁剪)。"""
        body: dict = {
            "instId": self.inst_id,
            "tdMode": self.td_mode,
            "side": self.side,
            "ordType": self.ord_type,
            "sz": self.sz,
        }
        if self.ord_type == "conditional":
            # 止盈止损:TP/SL 至少一个
            if self.tp_trigger_px:
                body["tpTriggerPx"] = self.tp_trigger_px
                body["tpOrdPx"] = self.tp_ord_px or "-1"
            if self.sl_trigger_px:
                body["slTriggerPx"] = self.sl_trigger_px
                body["slOrdPx"] = self.sl_ord_px or "-1"
        elif self.ord_type == "oco":
            if self.tp_trigger_px:
                body["tpTriggerPx"] = self.tp_trigger_px
                body["tpOrdPx"] = self.tp_ord_px or "-1"
            if self.sl_trigger_px:
                body["slTriggerPx"] = self.sl_trigger_px
                body["slOrdPx"] = self.sl_ord_px or "-1"
        elif self.ord_type == "trigger":
            body["triggerPx"] = self.trigger_px
            body["orderPx"] = self.order_px or "-1"  # -1=触发后市价
        elif self.ord_type == "move":
            body["callbackRatio"] = self.callback_ratio
            body["moveTriggerPx"] = self.move_trigger_px
            body["orderPx"] = self.order_px or "-1"
        if self.reduce_only:
            body["reduceOnly"] = True
        # 双向持仓模式(long_short)必须传 posSide;单向模式(net)传了会 51010
        if self.td_mode in ("cross", "isolated") and self.pos_mode == "long_short_mode":
            body["posSide"] = "long" if (self.side == "buy") != bool(self.reduce_only) else "short"
        if self.cl_ord_id:
            body["clOrdId"] = self.cl_ord_id
        return body


@dataclass
class AlgoExecutionRecord:
    algo_id: str = ""
    cl_ord_id: str = ""
    s_code: str = ""
    s_msg: str = ""
    okx_response: dict = field(default_factory=dict)


class AlgoOrderService:
    """策略委托服务。与现货下单服务平级,共享 creds/限流/日志。"""

    def __init__(self, creds: OKXCredentials, *, proxy: str = ""):
        self.creds = creds
        self.proxy = proxy

    # ---- 写 ----

    def place_algo_order(self, req: AlgoOrderRequest, db: Session) -> AlgoExecutionRecord:
        """提交策略委托。请求前先落库(审计),失败更新错误。写日志前缀 [OKX-RW]。"""
        req.validate()
        rec = AlgoExecutionRecord(cl_ord_id=req.cl_ord_id)
        row_id = _insert_algo_row(db, req)

        body = req.to_okx_body()
        logger.info(f"[OKX-RW] 提交策略委托 {req.inst_id} {req.ord_type} {req.side} sz={req.sz} body={body}")
        try:
            data = request(
                "POST", "/api/v5/trade/order-algo", self.creds,
                body=body, proxy=self.proxy, label="place_algo_order",
            )
            item = data[0] if isinstance(data, list) and data else {}
            rec.algo_id = str(item.get("algoId", ""))
            rec.s_code = str(item.get("sCode", ""))
            rec.s_msg = str(item.get("sMsg", ""))
            rec.okx_response = item
        except OKXAgentError as e:
            rec.s_code = e.code
            rec.s_msg = e.msg
            rec.okx_response = e.to_dict()
            _update_algo_row(db, row_id, rec)
            raise
        _update_algo_row(db, row_id, rec)
        if rec.s_code != "0":
            raise OKXAgentError(rec.s_code, rec.s_msg or "策略委托提交失败", data=rec.okx_response)
        return rec

    def cancel_algo_orders(self, items: list[dict]) -> list[dict]:
        """批量撤销:[{algo_id, inst_id}] → OKX cancel-algos。"""
        body = [{"algoId": i["algo_id"], "instId": i["inst_id"]} for i in items if i.get("algo_id")]
        if not body:
            raise OKXAgentError("INVALID_PARAM", "撤单列表为空")
        logger.info(f"[OKX-RW] 撤销策略委托 {body}")
        data = request(
            "POST", "/api/v5/trade/cancel-algos", self.creds,
            body=body, proxy=self.proxy, label="cancel_algos",
        )
        return data if isinstance(data, list) else []

    # ---- 读 ----

    def pending_algos(self, *, inst_type: str = "", inst_id: str = "", algo_id: str = "") -> list[dict]:
        params = {k: v for k, v in {
            "instType": inst_type, "instId": inst_id, "algoId": algo_id,
        }.items() if v}
        return request(
            "GET", "/api/v5/trade/orders-algo-pending", self.creds,
            params=params, proxy=self.proxy, label="ro_algo_pending",
        )

    def algo_history(self, *, inst_type: str = "", inst_id: str = "", algo_id: str = "", ord_type: str = "") -> list[dict]:
        """策略委托历史。ordType='triggered' 查已触发子单。"""
        params = {k: v for k, v in {
            "instType": inst_type, "instId": inst_id, "algoId": algo_id, "ordType": ord_type,
        }.items() if v}
        return request(
            "GET", "/api/v5/trade/orders-algo-history", self.creds,
            params=params, proxy=self.proxy, label="ro_algo_history",
        )


# ---------- 落库 ----------

def _insert_algo_row(db: Session, req: AlgoOrderRequest) -> int:
    from sqlalchemy import text as _t
    row = db.execute(
        _t("""
INSERT INTO okx_algo_orders
    (inst_id, td_mode, side, ord_type, sz, trigger_px, order_px,
     tp_trigger_px, tp_ord_px, sl_trigger_px, sl_ord_px,
     callback_ratio, move_trigger_px, reduce_only, cl_ord_id, status, created_at, updated_at)
VALUES (:iid, :td, :side, :ot, :sz, :tpx, :opx,
        :tptp, :tpop, :sltp, :slop,
        :cbr, :mtp, :ro, :cid, 'submitting', :now, :now)
"""),
        {
            "iid": req.inst_id, "td": req.td_mode, "side": req.side, "ot": req.ord_type,
            "sz": req.sz, "tpx": req.trigger_px, "opx": req.order_px,
            "tptp": req.tp_trigger_px, "tpop": req.tp_ord_px,
            "sltp": req.sl_trigger_px, "slop": req.sl_ord_px,
            "cbr": req.callback_ratio, "mtp": req.move_trigger_px,
            "ro": 1 if req.reduce_only else 0, "cid": req.cl_ord_id,
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    db.commit()
    return int(row.lastrowid or 0)


def _update_algo_row(db: Session, row_id: int, rec: AlgoExecutionRecord) -> None:
    import json as _json
    status = "live" if rec.s_code == "0" else "failed"
    db.execute(
        text("""
UPDATE okx_algo_orders
SET algo_id = :aid, status = :st, s_code = :sc, s_msg = :sm,
    okx_response = :resp, updated_at = :now
WHERE id = :rid
"""),
        {
            "aid": rec.algo_id or None, "st": status, "sc": rec.s_code, "sm": rec.s_msg,
            "resp": _json.dumps(rec.okx_response, ensure_ascii=False)[:4000],
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "rid": row_id,
        },
    )
    db.commit()


def list_algo_orders(db: Session, *, status: str = "", inst_id: str = "", limit: int = 50) -> list[dict]:
    """本地策略委托记录(含失败)。"""
    sql = """
SELECT id, inst_id, td_mode, side, ord_type, sz, trigger_px, order_px,
       tp_trigger_px, sl_trigger_px, algo_id, status, s_code, s_msg,
       okx_response, created_at, updated_at
FROM okx_algo_orders
"""
    conds, params = [], {"lim": min(max(limit, 1), 200)}
    if status:
        conds.append("status = :st"); params["st"] = status
    if inst_id:
        conds.append("inst_id = :iid"); params["iid"] = inst_id
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT :lim"
    rows = db.execute(text(sql), params).fetchall()
    cols = ["id", "inst_id", "td_mode", "side", "ord_type", "sz", "trigger_px", "order_px",
            "tp_trigger_px", "sl_trigger_px", "algo_id", "status", "s_code", "s_msg",
            "okx_response", "created_at", "updated_at"]
    return [dict(zip(cols, r)) for r in rows]
