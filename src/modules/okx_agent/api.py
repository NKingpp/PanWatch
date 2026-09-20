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
from src.modules.okx_agent.client import OKXAgentError, OKXCredentials, simulated_from_env
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
    simulated = simulated_from_env()
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
        simulated=simulated_from_env()
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

    幂等:同 inst_id 已在分析中 → 409。返回 trace_id,前端连
    GET /analyze/stream?trace_id= 实时观看各角色思考流。
    """
    inst_id = (body.inst_id or "").strip().upper()
    if not inst_id or "-" not in inst_id:
        raise HTTPException(400, "inst_id 必须是 OKX 交易对(如 BTC-USDT)")

    if ta_strategy.is_analyzing(inst_id):
        raise HTTPException(409, f"{inst_id} 深度分析进行中,请等待完成")

    from src.modules.automation.tradingagents.toolkit_adapter import is_crypto
    if not is_crypto(inst_id):
        raise HTTPException(400, f"{inst_id} 不是合法的 OKX 交易对")

    import time as _t
    trace_id = f"okx-ta-{inst_id}-{int(_t.time() * 1000)}"

    # 公共行情先验证交易对存在(未配置密钥也能用)
    def _run():
        asyncio.run(_run_ta_analysis(inst_id, trace_id))

    ta_strategy.spawn_analysis(inst_id, _run)
    return {"queued": True, "inst_id": inst_id, "trace_id": trace_id,
            "message": "深度分析已提交,预计 3-5 分钟"}


async def _run_ta_analysis(inst_id: str, trace_id: str) -> None:
    """后台跑 TradingAgents 深度分析 → 从 AnalysisHistory 读结果 → 落 pending 策略。"""
    from types import SimpleNamespace

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
            # 按账户资金自动决策仓位大小(现货:可用 USDT × 置信度映射比例)
            _auto_size_position(db, created)
            logger.info(f"[TA策略] {inst_id} 生成策略 #{created['id']} action={created['action']}")
        else:
            logger.info(f"[TA策略] {inst_id} 决策为 {sug.get('action')} ,无可执行策略")
    finally:
        db.close()


# 置信度 → 可用资金比例(现货,保守映射)
_CONF_PCT_MAP = (
    (8.0, 0.30),   # confidence ≥ 8 → 30%
    (6.0, 0.20),   # ≥ 6 → 20%
    (4.0, 0.10),   # ≥ 4 → 10%
)
_CONF_PCT_FLOOR = 0.05


def _conf_to_pct(confidence: float) -> float:
    for th, pct in _CONF_PCT_MAP:
        if confidence >= th:
            return pct
    return _CONF_PCT_FLOOR


def _auto_size_position(db: Session, strategy: dict) -> None:
    """分析完成后按账户剩余资金算 sz 写回策略。

    buy: 可用计价货币(USDT/USD) × 置信度比例 ÷ 现价,对齐 lotSz/minSz;
    sell: 有现货持仓则全仓卖出(受同规则对齐),无持仓则留空由人工确认时提示。
    密钥未配置/查询失败:sz 留空,approve 时要求人工补。
    """
    inst_id = strategy["inst_id"]
    action = strategy["action"]
    sid = strategy["id"]

    try:
        simulated = simulated_from_env()
        creds = OKXCredentials.from_env(simulated=simulated)
        if not creds:
            logger.info(f"[TA策略] #{sid} 未配置 OKX 密钥,跳过自动仓位计算")
            return
        proxy_row = db.execute(text("SELECT value FROM app_settings WHERE key='http_proxy'")).first()
        from src.modules.okx_agent.account import AccountQueryService
        svc = AccountQueryService(creds, proxy=(proxy_row[0] if proxy_row else "") or "")

        spec = svc.instrument_spec(inst_id)
        if spec.inst_type != "SPOT":
            logger.info(f"[TA策略] #{sid} {inst_id} 非现货(instType={spec.inst_type}),跳过自动仓位")
            return
        snap = svc.snapshot(inst_id, td_mode="cash")
        base_ccy, quote_ccy = inst_id.split("-", 1)
        last = snap.last_px or 0
        if last <= 0:
            logger.warning(f"[TA策略] #{sid} 无最新价,跳过自动仓位")
            return

        sz = ""
        if action == "buy":
            avail = snap.available_ccy.get(quote_ccy) or 0
            pct = _conf_to_pct(float(strategy["confidence"] or 0))
            budget = avail * pct
            raw = budget / last
            sz = _align_sz(raw, spec.lot_sz, spec.min_sz)
            if not sz:
                logger.warning(
                    f"[TA策略] #{sid} buy 可用{quote_ccy}={avail:.2f}×{pct:.0%}=${budget:.2f}"
                    f" 不足最小下单量({spec.min_sz})"
                )
        elif action == "sell":
            pos = snap.position_of(inst_id)
            held = 0.0
            if pos:
                try:
                    held = abs(float(pos.get("pos") or 0))
                except (TypeError, ValueError):
                    held = 0.0
            if held > 0:
                sz = _align_sz(held, spec.lot_sz, spec.min_sz)
            else:
                logger.info(f"[TA策略] #{sid} sell 但无 {inst_id} 持仓,sz 留空")

        if sz:
            db.execute(
                text("UPDATE ta_trade_strategies SET sz = :sz, updated_at = CURRENT_TIMESTAMP WHERE id = :i"),
                {"sz": sz, "i": sid},
            )
            db.commit()
            logger.info(f"[TA策略] #{sid} 自动仓位 sz={sz}({action}, last={last})")
    except OKXAgentError as e:
        logger.warning(f"[TA策略] #{sid} 自动仓位计算失败(不影响策略生成): {e.msg}")
    except Exception as e:
        logger.warning(f"[TA策略] #{sid} 自动仓位计算异常(不影响策略生成): {e}")


def _align_sz(raw: float, lot_sz: float, min_sz: float) -> str:
    """对齐 lotSz 步长;低于 minSz 返回空。"""
    if raw <= 0:
        return ""
    step = lot_sz if lot_sz > 0 else (min_sz if min_sz > 0 else 0)
    if step <= 0:
        s = f"{raw:.6f}".rstrip("0").rstrip(".")
        return s if (min_sz or 0) == 0 or raw >= min_sz else ""
    import math
    decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
    aligned = math.floor(raw / step) * step
    if aligned < (min_sz or 0) or aligned <= 0:
        return ""
    return f"{aligned:.{decimals}f}" if decimals > 0 else str(int(round(aligned)))


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


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


# 进度 SSE 复用 automation 模块的节奏/终态约定
PROGRESS_SSE_POLL_SEC = 1.0
PROGRESS_SSE_MAX_DURATION_SEC = 30 * 60


@router.get("/analyze/stream")
async def stream_analyze_progress(trace_id: str):
    """AI 策略分析实时流(SSE)。

    事件:
    - progress: 完整进度快照(status/stages/events,结构同
      GET /api/agents/runs/{trace_id}/progress),快照变化即推;
    - done: 终态(success/failed/stale),关流。

    前端用 fetch 流式读取(带 Authorization header,原生 EventSource 不支持)。
    """
    import json as _json
    import time as _time

    from fastapi.responses import StreamingResponse
    from src.platform.events.sse import format_sse_comment, format_sse_event
    from src.platform.persistence.database import SessionLocal
    from src.modules.automation.tradingagents.progress import build_progress_snapshot

    if not trace_id or len(trace_id) > 64:
        raise HTTPException(400, "无效的 trace_id")

    def _snapshot() -> dict:
        db = SessionLocal()
        try:
            return build_progress_snapshot(db, trace_id)
        finally:
            db.close()

    async def gen():
        seq = 0
        last_payload = ""
        started = _time.monotonic()
        idle_ticks = 0
        while _time.monotonic() - started < PROGRESS_SSE_MAX_DURATION_SEC:
            try:
                progress = await asyncio.to_thread(_snapshot)
            except Exception as e:
                logger.warning(f"[TA策略] 进度 SSE 快照失败: {e}")
                await asyncio.sleep(PROGRESS_SSE_POLL_SEC)
                continue

            payload = _json.dumps(progress, ensure_ascii=False, default=str)
            if payload != last_payload:
                last_payload = payload
                seq += 1
                idle_ticks = 0
                yield format_sse_event(seq, "progress", payload)
            else:
                idle_ticks += 1
                if idle_ticks >= 15:
                    idle_ticks = 0
                    yield format_sse_comment()

            if progress.get("status") in ("success", "failed", "stale", "not_found"):
                seq += 1
                yield format_sse_event(seq, "done", {"status": progress.get("status")})
                return
            await asyncio.sleep(PROGRESS_SSE_POLL_SEC)

        seq += 1
        yield format_sse_event(seq, "done", {"status": "timeout"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ApproveBody(BaseModel):
    sz: str = ""                 # 可选:空 = 用自动仓位计算结果
    ord_type: str = "market"
    px: str = ""
    td_mode: str = "cash"


@router.post("/strategies/{strategy_id}/approve")
def approve_strategy(strategy_id: int, body: ApproveBody, db: Session = Depends(get_db)):
    """确认执行策略:调 OKX 下单。总开关关 → 403;策略非 pending → 409。

    sz 为空时用策略预计算的自动仓位(按账户剩余资金 × 置信度比例)。
    """
    if not _agent_enabled(db):
        raise HTTPException(403, "Agent 自动交易已关闭,无法执行策略")
    rec = ta_strategy.get_strategy(db, strategy_id)
    if not rec:
        raise HTTPException(404, "策略不存在")
    if rec["status"] != "pending":
        raise HTTPException(409, f"策略状态为 {rec['status']},仅 pending 可执行")

    sz = (body.sz or "").strip() or (rec.get("sz") or "").strip()
    if not sz or _to_float(sz) <= 0:
        raise HTTPException(400, "策略无可用数量(自动仓位计算未出结果),请稍后重试或拒绝该策略")

    svc = _get_service(db)
    req = AgentOrderRequest(
        inst_id=rec["inst_id"],
        side=rec["action"],          # buy / sell
        ord_type=body.ord_type,
        sz=sz,
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


# ==================== 策略委托(Algo Trading) ====================
# 分层:account(查询)/ risk(校验)/ proposal(人工确认)/ algo(下单)
# 范围:仅策略委托;所有真实下单必须经 confirm,不支持免确认。

from src.modules.okx_agent import algo as okx_algo
from src.modules.okx_agent import proposal as okx_proposal
from src.modules.okx_agent.account import AccountQueryService
from src.modules.okx_agent.algo import AlgoOrderService
from src.modules.okx_agent.risk import RiskConfig, load_risk_config, save_risk_config


def _get_account_svc(db: Session) -> AccountQueryService:
    simulated = simulated_from_env()
    creds = OKXCredentials.from_env(simulated=simulated)
    if not creds:
        raise HTTPException(503, "OKX Agent 密钥未配置(需 OKX_AGENT_API_KEY/SECRET_KEY/PASSPHRASE 环境变量)")
    proxy_row = db.execute(text("SELECT value FROM app_settings WHERE key='http_proxy'")).first()
    return AccountQueryService(creds, proxy=(proxy_row[0] if proxy_row else "") or "")


def _get_algo_svc(db: Session) -> AlgoOrderService:
    simulated = simulated_from_env()
    creds = OKXCredentials.from_env(simulated=simulated)
    if not creds:
        raise HTTPException(503, "OKX Agent 密钥未配置")
    proxy_row = db.execute(text("SELECT value FROM app_settings WHERE key='http_proxy'")).first()
    return AlgoOrderService(creds, proxy=(proxy_row[0] if proxy_row else "") or "")


class AlgoProposalBody(BaseModel):
    """创建策略委托提案。ordType 决定哪些价格字段有效。"""
    inst_id: str
    td_mode: str = "cash"
    side: str
    ord_type: str                      # conditional / oco / trigger / move
    sz: str
    order_px: str = ""                 # 委托价(空=市价)
    trigger_px: str = ""               # trigger 必填
    tp_trigger_px: str = ""            # conditional/oco 止盈触发
    tp_ord_px: str = "-1"
    sl_trigger_px: str = ""            # conditional/oco 止损触发
    sl_ord_px: str = "-1"
    callback_ratio: str = ""           # move 必填
    move_trigger_px: str = ""          # move 必填
    reduce_only: bool = False


@router.post("/algo/proposal")
def create_algo_proposal(body: AlgoProposalBody, db: Session = Depends(get_db)):
    """创建策略委托提案:拉账户快照 → 风控校验 → pending_confirm(等人工)。

    风控不通过直接返回 risk_failed(不进入确认流)。需 OKX 密钥(读账户)。
    """
    if not _agent_enabled(db):
        raise HTTPException(403, "Agent 自动交易已关闭")
    # 参数校验先于密钥检查(快速失败,不用等账户服务构造)
    from src.modules.okx_agent.algo import AlgoOrderRequest
    try:
        AlgoOrderRequest(
            inst_id=body.inst_id, td_mode=body.td_mode, side=body.side,
            ord_type=body.ord_type, sz=body.sz, trigger_px=body.trigger_px,
            tp_trigger_px=body.tp_trigger_px, sl_trigger_px=body.sl_trigger_px,
            callback_ratio=body.callback_ratio, move_trigger_px=body.move_trigger_px,
        ).validate()
    except OKXAgentError as e:
        raise _okx_error_response(e)
    cfg = load_risk_config(db)
    if not cfg.enabled:
        raise HTTPException(403, "策略委托模块已被风控配置禁用")
    try:
        account_svc = _get_account_svc(db)
        outcome = okx_proposal.create_proposal(db, body.model_dump(), account_svc=account_svc, cfg=cfg)
    except OKXAgentError as e:
        raise _okx_error_response(e)
    return {"proposal": outcome.proposal, "risk": outcome.risk, "ok": outcome.ok, "message": outcome.message}


@router.get("/algo/proposals")
def list_algo_proposals(status: str = "", inst_id: str = "", limit: int = 50, db: Session = Depends(get_db)):
    return okx_proposal.list_proposals(db, status=status, inst_id=inst_id, limit=limit)


@router.get("/algo/proposals/{pid}")
def get_algo_proposal(pid: int, db: Session = Depends(get_db)):
    p = okx_proposal.get_proposal(db, pid)
    if not p:
        raise HTTPException(404, "提案不存在")
    return p


@router.post("/algo/proposals/{pid}/confirm")
def confirm_algo_proposal(pid: int, db: Session = Depends(get_db)):
    """人工确认:重新拉账户 + 重新风控 → 通过才调 OKX order-algo。"""
    if not _agent_enabled(db):
        raise HTTPException(403, "Agent 自动交易已关闭")
    cfg = load_risk_config(db)
    try:
        account_svc = _get_account_svc(db)
        algo_svc = _get_algo_svc(db)
        outcome = okx_proposal.confirm_proposal(
            db, pid, account_svc=account_svc, algo_svc=algo_svc, cfg=cfg
        )
    except OKXAgentError as e:
        raise _okx_error_response(e)
    return {"proposal": outcome.proposal, "risk": outcome.risk, "ok": outcome.ok, "message": outcome.message}


@router.post("/algo/proposals/{pid}/reject")
def reject_algo_proposal(pid: int, db: Session = Depends(get_db)):
    try:
        return okx_proposal.reject_proposal(db, pid)
    except OKXAgentError as e:
        raise _okx_error_response(e)


@router.get("/algo/orders/pending")
def algo_pending(inst_type: str = "", inst_id: str = "", algo_id: str = "", db: Session = Depends(get_db)):
    """OKX 侧未触发策略委托(实时)。"""
    try:
        return _get_algo_svc(db).pending_algos(inst_type=inst_type, inst_id=inst_id, algo_id=algo_id)
    except OKXAgentError as e:
        raise _okx_error_response(e)


@router.get("/algo/orders/history")
def algo_history(inst_type: str = "", inst_id: str = "", algo_id: str = "", ord_type: str = "", db: Session = Depends(get_db)):
    """OKX 侧策略委托历史(ord_type=triggered 查已触发的子单)。"""
    try:
        return _get_algo_svc(db).algo_history(inst_type=inst_type, inst_id=inst_id, algo_id=algo_id, ord_type=ord_type)
    except OKXAgentError as e:
        raise _okx_error_response(e)


@router.get("/algo/orders/local")
def algo_local(status: str = "", inst_id: str = "", limit: int = 50, db: Session = Depends(get_db)):
    """本地策略委托记录(含失败)。"""
    return okx_algo.list_algo_orders(db, status=status, inst_id=inst_id, limit=limit)


class AlgoCancelBody(BaseModel):
    items: list[dict]                  # [{algo_id, inst_id}]


@router.post("/algo/orders/cancel")
def algo_cancel(body: AlgoCancelBody, db: Session = Depends(get_db)):
    """撤销 OKX 侧策略委托。"""
    if not _agent_enabled(db):
        raise HTTPException(403, "Agent 自动交易已关闭")
    try:
        return _get_algo_svc(db).cancel_algo_orders(body.items)
    except OKXAgentError as e:
        raise _okx_error_response(e)


@router.get("/algo/risk-config")
def get_algo_risk_config(db: Session = Depends(get_db)):
    return load_risk_config(db).to_dict()


class RiskConfigBody(BaseModel):
    enabled: bool | None = None
    max_notional_usd: float | None = None
    max_notional_pct: float | None = None
    max_margin_pct: float | None = None
    max_total_margin_pct: float | None = None
    imr_hard_limit: float | None = None
    fee_buffer_pct: float | None = None
    min_net_usd: float | None = None


@router.put("/algo/risk-config")
def put_algo_risk_config(body: RiskConfigBody, db: Session = Depends(get_db)):
    """更新风控阈值(部分更新)。enabled=False 快速禁用整个策略委托模块。"""
    cfg = load_risk_config(db)
    d = body.model_dump(exclude_none=True)
    if "enabled" in d:
        cfg.enabled = d.pop("enabled")
    for k, v in d.items():
        if hasattr(cfg, k) and v is not None:
            setattr(cfg, k, v)
    if cfg.max_notional_usd <= 0 or cfg.max_notional_pct <= 0 or cfg.max_margin_pct <= 0:
        raise HTTPException(400, "阈值必须大于 0")
    save_risk_config(db, cfg)
    return cfg.to_dict()
