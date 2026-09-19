"""OKX Agent 自动交易 HTTP API。

- 总开关在 app_settings(okx_agent_enabled),关闭时所有写接口 403;
- 密钥从 env 读,不接收前端传入的密钥字段;
- 读接口(状态/余额/持仓)开关关闭时仍可用;
- /analyze + /strategies:TradingAgents 多 Agent 决策 → 策略 → 人工确认执行。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.modules.okx_agent import strategy as ta_strategy
from src.modules.okx_agent.client import OKXAgentError, OKXCredentials
from src.modules.okx_agent.service import AgentOrderRequest, OKXAgentService
from src.platform.persistence.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

_SETTING_KEY = "okx_agent_enabled"


def _agent_enabled(db: Session) -> bool:
    row = db.execute(
        text("SELECT value FROM app_settings WHERE key = :k"), {"k": _SETTING_KEY}
    ).first()
    return bool(row and str(row[0]).lower() in ("1", "true", "yes"))


def _get_service(db: Session) -> OKXAgentService:
    """从 env 构造服务实例;缺密钥抛 503。proxy 读 http_proxy 设置。"""
    simulated = os.environ.get("OKX_AGENT_SIMULATED", "").lower() in ("1", "true", "yes")
    creds = OKXCredentials.from_env(simulated=simulated)
    if not creds:
        raise HTTPException(503, "OKX Agent 密钥未配置(需 OKX_AGENT_API_KEY/SECRET_KEY/PASSPHRASE 环境变量)")
    proxy_row = db.execute(text("SELECT value FROM app_settings WHERE key='http_proxy'")).first()
    return OKXAgentService(creds, enabled=_agent_enabled(db), proxy=(proxy_row[0] if proxy_row else "") or "")


def _okx_error_response(e: OKXAgentError):
    """OKX 业务/网络错误 → 结构化 400(策略层可读 code/msg/data)。"""
    return HTTPException(400, detail={"code": e.code, "msg": e.msg, "data": e.data})


# ---------- 配置 ----------

class ToggleBody(BaseModel):
    enabled: bool


@router.get("/status")
def agent_status(db: Session = Depends(get_db)):
    """开关状态 + 密钥配置情况(不回传密钥本体)。"""
    creds = OKXCredentials.from_env(
        simulated=os.environ.get("OKX_AGENT_SIMULATED", "").lower() in ("1", "true")
    )
    return {
        "enabled": _agent_enabled(db),
        "credentials_configured": creds is not None,
        "simulated": creds.simulated if creds else False,
    }


@router.post("/toggle")
def toggle_agent(body: ToggleBody, db: Session = Depends(get_db)):
    """启停 Agent 自动交易总开关。"""
    db.execute(
        text("""
INSERT INTO app_settings (key, value, description)
VALUES (:k, :v, 'OKX Agent 自动交易总开关')
ON CONFLICT(key) DO UPDATE SET value = :v
"""),
        {"k": _SETTING_KEY, "v": "1" if body.enabled else "0"},
    )
    db.commit()
    return {"enabled": body.enabled}


# ---------- 写接口(交易) ----------

class OrderBody(BaseModel):
    inst_id: str
    side: str
    ord_type: str = "market"
    sz: str
    px: str = ""
    td_mode: str = "cash"
    cl_ord_id: str = ""
    reduce_only: bool | str = ""


@router.post("/orders")
def place_order(body: OrderBody, db: Session = Depends(get_db)):
    """Agent 下单。总开关关闭 → 403。"""
    if not _agent_enabled(db):
        raise HTTPException(403, "Agent 自动交易已关闭")
    svc = _get_service(db)
    req = AgentOrderRequest(
        inst_id=body.inst_id, side=body.side, ord_type=body.ord_type, sz=body.sz,
        px=body.px, td_mode=body.td_mode, cl_ord_id=body.cl_ord_id, reduce_only=body.reduce_only,
    )
    try:
        rec = svc.place_order(req, db, account_id=0)
    except OKXAgentError as e:
        raise _okx_error_response(e)
    return {
        "ord_id": rec.ord_id, "cl_ord_id": rec.cl_ord_id,
        "status": rec.status, "okx": rec.okx_response,
    }


class CancelBody(BaseModel):
    inst_id: str
    ord_id: str = ""
    cl_ord_id: str = ""


@router.post("/orders/cancel")
def cancel_order(body: CancelBody, db: Session = Depends(get_db)):
    if not _agent_enabled(db):
        raise HTTPException(403, "Agent 自动交易已关闭")
    svc = _get_service(db)
    try:
        item = svc.cancel_order(body.inst_id, body.ord_id, body.cl_ord_id)
    except OKXAgentError as e:
        raise _okx_error_response(e)
    return item


# ---------- 读接口(状态查询) ----------

@router.get("/orders")
def query_orders(
    inst_id: str = "", cl_ord_id: str = "", ord_id: str = "",
    db: Session = Depends(get_db),
):
    """查 OKX 侧订单执行状态(实时)。"""
    svc = _get_service(db)
    try:
        data = svc.order_status(inst_id=inst_id, cl_ord_id=cl_ord_id, ord_id=ord_id)
    except OKXAgentError as e:
        raise _okx_error_response(e)
    return data


@router.get("/orders/pending")
def pending_orders(inst_type: str = "", inst_id: str = "", db: Session = Depends(get_db)):
    svc = _get_service(db)
    try:
        return svc.pending_orders(inst_type, inst_id)
    except OKXAgentError as e:
        raise _okx_error_response(e)


@router.get("/history")
def local_history(limit: int = 50, db: Session = Depends(get_db)):
    """本地落库的 Agent 执行记录(含失败单)。"""
    rows = db.execute(
        text("""
SELECT id, inst_id, side, ord_type, td_mode, sz, px, cl_ord_id, ord_id,
       status, error_code, error_msg, created_at, updated_at
FROM okx_agent_orders
ORDER BY id DESC LIMIT :lim
"""),
        {"lim": min(max(limit, 1), 200)},
    ).fetchall()
    cols = ["id", "inst_id", "side", "ord_type", "td_mode", "sz", "px", "cl_ord_id",
            "ord_id", "status", "error_code", "error_msg", "created_at", "updated_at"]
    return [dict(zip(cols, r)) for r in rows]


@router.get("/balance")
def balance(ccy: str = "", db: Session = Depends(get_db)):
    svc = _get_service(db)
    try:
        return svc.account_balance(ccy)
    except OKXAgentError as e:
        raise _okx_error_response(e)


@router.get("/positions")
def positions(inst_type: str = "", inst_id: str = "", db: Session = Depends(get_db)):
    svc = _get_service(db)
    try:
        return svc.positions(inst_type, inst_id)
    except OKXAgentError as e:
        raise _okx_error_response(e)


# ---------- TradingAgents 多 Agent 决策 → 策略 ----------


class AnalyzeBody(BaseModel):
    inst_id: str


@router.post("/analyze")
def analyze_inst(body: AnalyzeBody, db: Session = Depends(get_db)):
    """触发 TradingAgents 多 Agent 深度分析(后台 3-5 分钟),完成后生成待确认策略。

    幂等:同 inst_id 已在分析中 → 409。前端轮询 GET /strategies?inst_id= 等结果。
    """
    inst_id = (body.inst_id or "").strip().upper()
    if not inst_id or "-" not in inst_id:
        raise HTTPException(400, "inst_id 必须是 OKX 交易对(如 BTC-USDT)")

    if ta_strategy.is_analyzing(inst_id):
        raise HTTPException(409, f"{inst_id} 深度分析进行中,请等待完成")

    from src.modules.automation.tradingagents.toolkit_adapter import is_crypto
    if not is_crypto(inst_id):
        raise HTTPException(400, f"{inst_id} 不是合法的 OKX 交易对")

    # 公共行情先验证交易对存在(未配置密钥也能用)
    def _run():
        asyncio.run(_run_ta_analysis(inst_id))

    ta_strategy.spawn_analysis(inst_id, _run)
    return {"queued": True, "inst_id": inst_id, "message": "深度分析已提交,预计 3-5 分钟"}


async def _run_ta_analysis(inst_id: str) -> None:
    """后台跑 TradingAgents 深度分析 → 从 AnalysisHistory 读结果 → 落 pending 策略。"""
    import time as _time
    from types import SimpleNamespace

    trace_id = f"okx-ta-{inst_id}-{int(_time.time() * 1000)}"
    stock = SimpleNamespace(id=0, symbol=inst_id, name=inst_id, market="CRYPTO")
    try:
        from server import trigger_agent_for_stock
        await trigger_agent_for_stock(
            "tradingagents",
            stock,
            suppress_notify=True,      # 策略场景不推通知,结果由策略面板展示
            trace_id=trace_id,
            force_refresh=True,        # 每次分析都要新鲜决策
        )
    except Exception as e:
        logger.error(f"[TA策略] {inst_id} 深度分析失败: {e}")
        return

    # 分析完成 → 读最新结果生成策略
    from src.platform.persistence.database import SessionLocal
    db = SessionLocal()
    try:
        from src.platform.persistence.models import AnalysisHistory
        from datetime import date as _date
        rec = (
            db.query(AnalysisHistory)
            .filter(
                AnalysisHistory.agent_name == "tradingagents",
                AnalysisHistory.stock_symbol == inst_id,
            )
            .order_by(AnalysisHistory.updated_at.desc(), AnalysisHistory.id.desc())
            .first()
        )
        if not rec or not rec.raw_data:
            logger.warning(f"[TA策略] {inst_id} 分析完成但无结果记录")
            return
        analysis = {
            "raw_data": rec.raw_data or {},
            "analysis_date": (rec.analysis_date or _date.today()).isoformat()
            if not isinstance(rec.analysis_date, str) else str(rec.analysis_date),
            "trace_id": trace_id,
        }
        sug = (rec.raw_data or {}).get("suggestion") or {}
        created = ta_strategy.create_strategy_from_analysis(db, inst_id, analysis)
        if created:
            logger.info(f"[TA策略] {inst_id} 生成策略 #{created['id']} action={created['action']}")
        else:
            logger.info(f"[TA策略] {inst_id} 决策为 {sug.get('action')} ,无可执行策略")
    finally:
        db.close()


@router.get("/strategies")
def list_strategies(
    status: str = "", inst_id: str = "", limit: int = 50,
    db: Session = Depends(get_db),
):
    """策略列表。前端分析完成后轮询 pending 或全部。"""
    return ta_strategy.list_strategies(db, status=status, inst_id=inst_id, limit=limit)


@router.get("/analyze/status")
def analyze_status(inst_id: str):
    """查询某交易对是否在分析中(前端按钮态)。"""
    iid = (inst_id or "").strip().upper()
    return {"inst_id": iid, "analyzing": ta_strategy.is_analyzing(iid)}


class ApproveBody(BaseModel):
    sz: str                  # 数量(用户审批时填)
    ord_type: str = "market"
    px: str = ""
    td_mode: str = "cash"


@router.post("/strategies/{strategy_id}/approve")
def approve_strategy(strategy_id: int, body: ApproveBody, db: Session = Depends(get_db)):
    """确认执行策略:调 OKX 下单。总开关关 → 403;策略非 pending → 409。"""
    if not _agent_enabled(db):
        raise HTTPException(403, "Agent 自动交易已关闭,无法执行策略")
    rec = ta_strategy.get_strategy(db, strategy_id)
    if not rec:
        raise HTTPException(404, "策略不存在")
    if rec["status"] != "pending":
        raise HTTPException(409, f"策略状态为 {rec['status']},仅 pending 可执行")

    svc = _get_service(db)
    req = AgentOrderRequest(
        inst_id=rec["inst_id"],
        side=rec["action"],          # buy / sell
        ord_type=body.ord_type,
        sz=body.sz,
        px=body.px,
        td_mode=body.td_mode,
        cl_ord_id=f"ta-s{strategy_id}",
    )
    try:
        import json as _json
        result = svc.place_order(req, db, account_id=0)
        ta_strategy.update_strategy_status(
            db, strategy_id, "executed",
            ord_id=result.ord_id, okx_response=_json.dumps(result.okx_response, ensure_ascii=False),
        )
        return {"strategy_id": strategy_id, "ord_id": result.ord_id, "status": result.status}
    except OKXAgentError as e:
        ta_strategy.update_strategy_status(db, strategy_id, "failed", error_msg=f"{e.code}: {e.msg}")
        raise _okx_error_response(e)


@router.post("/strategies/{strategy_id}/reject")
def reject_strategy(strategy_id: int, db: Session = Depends(get_db)):
    """拒绝策略(仅 pending 可拒)。"""
    rec = ta_strategy.get_strategy(db, strategy_id)
    if not rec:
        raise HTTPException(404, "策略不存在")
    if rec["status"] != "pending":
        raise HTTPException(409, f"策略状态为 {rec['status']},仅 pending 可拒绝")
    ta_strategy.update_strategy_status(db, strategy_id, "rejected")
    return {"strategy_id": strategy_id, "status": "rejected"}
