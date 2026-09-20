"""人工确认层:策略委托提案生命周期管理。

流程:
  POST /algo/proposal      → 风控快照校验 → 通过:落 pending_confirm(等人工确认)
                              不通过:落 risk_failed(返回违规明细,禁止下单)
  POST /algo/p/:id/confirm → **重新拉账户+重新风控** → 通过:调 order-algo 下单
                              不通过:落 risk_failed(返回违规明细)
  POST /algo/p/:id/reject  → 人工拒绝 → rejected
  TTL 超时                 → expired(不自动下单)

设计要点:确认时刻的账户状态可能与创建时刻不同(资金变动/持仓变动/杠杆变更),
所以 confirm 必须重跑完整风控,任何时刻校验失败都不发起下单。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.modules.okx_agent.account import AccountQueryService, AccountSnapshot
from src.modules.okx_agent.algo import AlgoOrderRequest, AlgoOrderService
from src.modules.okx_agent.client import OKXAgentError
from src.modules.okx_agent.risk import RiskChecker, RiskConfig, TradeIntent

logger = logging.getLogger(__name__)

PROPOSAL_TTL_HOURS = 24

_COLS = (
    "id, inst_id, td_mode, side, ord_type, sz, order_px, tp_trigger_px, tp_ord_px, "
    "sl_trigger_px, sl_ord_px, trigger_px, callback_ratio, move_trigger_px, reduce_only, "
    "status, algo_id, s_code, s_msg, risk_check, snapshot_before, snapshot_after, "
    "created_at, updated_at"
)


def _row_to_dict(row) -> dict:
    cols = [c.strip() for c in _COLS.split(",")]
    return dict(zip(cols, row))


def get_proposal(db: Session, pid: int) -> dict | None:
    rows = db.execute(
        text(f"SELECT {_COLS} FROM okx_algo_proposals WHERE id = :i"), {"i": pid}
    ).fetchall()
    return _row_to_dict(rows[0]) if rows else None


def list_proposals(db: Session, *, status: str = "", inst_id: str = "", limit: int = 50) -> list[dict]:
    _expire_stale(db)
    sql = f"SELECT {_COLS} FROM okx_algo_proposals"
    conds, params = [], {"lim": min(max(limit, 1), 200)}
    if status:
        conds.append("status = :st"); params["st"] = status
    if inst_id:
        conds.append("inst_id = :iid"); params["iid"] = inst_id
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT :lim"
    rows = db.execute(text(sql), params).fetchall()
    return [_row_to_dict(r) for r in rows]


def _expire_stale(db: Session) -> None:
    db.execute(
        text("""
UPDATE okx_algo_proposals
SET status = 'expired', updated_at = CURRENT_TIMESTAMP
WHERE status = 'pending_confirm'
  AND created_at < DATETIME('now', :neg || ' hours')
"""),
        {"neg": -PROPOSAL_TTL_HOURS},
    )
    db.commit()


def _intent_from_body(body: dict) -> TradeIntent:
    return TradeIntent(
        inst_id=body["inst_id"], side=body["side"], td_mode=body.get("td_mode", "cash"),
        sz=body["sz"], px=body.get("order_px", ""), reduce_only=bool(body.get("reduce_only", False)),
    )


def _algo_req_from_body(body: dict) -> AlgoOrderRequest:
    return AlgoOrderRequest(
        inst_id=body["inst_id"], td_mode=body.get("td_mode", "cash"), side=body["side"],
        ord_type=body["ord_type"], sz=body["sz"],
        order_px=body.get("order_px", ""), trigger_px=body.get("trigger_px", ""),
        tp_trigger_px=body.get("tp_trigger_px", ""), tp_ord_px=body.get("tp_ord_px", "-1"),
        sl_trigger_px=body.get("sl_trigger_px", ""), sl_ord_px=body.get("sl_ord_px", "-1"),
        callback_ratio=body.get("callback_ratio", ""), move_trigger_px=body.get("move_trigger_px", ""),
        reduce_only=bool(body.get("reduce_only", False)),
    )


def _intent_from_proposal(p: dict) -> TradeIntent:
    return TradeIntent(
        inst_id=p["inst_id"], side=p["side"], td_mode=p["td_mode"],
        sz=p["sz"], px=p["order_px"] or "",
        reduce_only=bool(p["reduce_only"]),
    )


def _algo_req_from_proposal(p: dict) -> AlgoOrderRequest:
    return AlgoOrderRequest(
        inst_id=p["inst_id"], td_mode=p["td_mode"], side=p["side"], ord_type=p["ord_type"],
        sz=p["sz"], trigger_px=p["trigger_px"] or "", order_px=p["order_px"] or "",
        tp_trigger_px=p["tp_trigger_px"] or "", tp_ord_px=p["tp_ord_px"] or "-1",
        sl_trigger_px=p["sl_trigger_px"] or "", sl_ord_px=p["sl_ord_px"] or "-1",
        callback_ratio=p["callback_ratio"] or "", move_trigger_px=p["move_trigger_px"] or "",
        reduce_only=bool(p["reduce_only"]),
    )


def _snapshot_brief(snap: AccountSnapshot) -> dict:
    """落库的快照摘要(全字段,供前端确认页展示与审计)。"""
    return {
        "total_eq_usd": snap.total_eq_usd,
        "available_ccy": {k: round(v, 6) for k, v in snap.available_ccy.items() if v > 0},
        "imr": snap.imr, "mmr": snap.mmr, "mgn_ratio": snap.mgn_ratio,
        "lever": snap.lever, "lever_hint": snap.lever_hint,
        "positions_count": len(snap.positions),
        "inst_position": _pos_brief(snap.position_of(snap.spec.inst_id if snap.spec else "")),
        "last_px": snap.last_px,
        "spec": {
            "inst_type": snap.spec.inst_type, "ct_val": snap.spec.ct_val,
            "lot_sz": snap.spec.lot_sz, "min_sz": snap.spec.min_sz,
        } if snap.spec else None,
    }


def _pos_brief(p: dict | None) -> dict | None:
    if not p:
        return None
    return {
        "pos": p.get("pos"), "avgPx": p.get("avgPx"), "lever": p.get("lever"),
        "mgnMode": p.get("mgnMode"), "liab": p.get("liab"), "imr": p.get("imr"),
    }


def _update_status(
    db: Session, pid: int, status: str, *,
    algo_id: str = "", s_code: str = "", s_msg: str = "",
    risk_check: str = "", snapshot_after: str = "",
) -> None:
    db.execute(
        text("""
UPDATE okx_algo_proposals
SET status = :st, algo_id = :aid, s_code = :sc, s_msg = :sm,
    risk_check = :rc, snapshot_after = :sa, updated_at = CURRENT_TIMESTAMP
WHERE id = :i
"""),
        {
            "st": status, "aid": algo_id or None, "sc": s_code, "sm": s_msg,
            "rc": risk_check[:8000] or None, "sa": snapshot_after[:8000] or None, "i": pid,
        },
    )
    db.commit()


@dataclass
class ProposalOutcome:
    """创建/确认的结果:统一结构给 API 层。"""
    proposal: dict
    risk: dict | None = None       # RiskCheckResult.to_dict()
    ok: bool = True
    message: str = ""


def create_proposal(
    db: Session, body: dict, *,
    account_svc: AccountQueryService, cfg: RiskConfig,
) -> ProposalOutcome:
    """创建提案:拉快照 → 风控 → pending_confirm / risk_failed。"""
    intent = _intent_from_body(body)
    # 先做参数级校验(复用 AlgoOrderRequest.validate 的 ordType 校验逻辑)
    probe = _algo_req_from_body(body)
    probe.validate()

    snap = account_svc.snapshot(intent.inst_id, td_mode=intent.td_mode)
    risk = RiskChecker(cfg).validate(snap, intent)
    risk_dict = risk.to_dict()

    row = db.execute(
        text("""
INSERT INTO okx_algo_proposals
    (inst_id, td_mode, side, ord_type, sz, order_px, tp_trigger_px, tp_ord_px,
     sl_trigger_px, sl_ord_px, trigger_px, callback_ratio, move_trigger_px,
     reduce_only, status, risk_check, snapshot_before)
VALUES (:iid, :td, :side, :ot, :sz, :opx, :tptp, :tpop, :sltp, :slop,
        :tpx, :cbr, :mtp, :ro, :st, :rc, :sb)
"""),
        {
            "iid": intent.inst_id, "td": intent.td_mode, "side": intent.side,
            "ot": probe.ord_type, "sz": intent.sz, "opx": body.get("order_px", ""),
            "tptp": body.get("tp_trigger_px", ""), "tpop": body.get("tp_ord_px", "-1"),
            "sltp": body.get("sl_trigger_px", ""), "slop": body.get("sl_ord_px", "-1"),
            "tpx": body.get("trigger_px", ""), "cbr": body.get("callback_ratio", ""),
            "mtp": body.get("move_trigger_px", ""),
            "ro": 1 if intent.reduce_only else 0,
            "st": "pending_confirm" if risk.ok else "risk_failed",
            "rc": json.dumps(risk_dict, ensure_ascii=False),
            "sb": json.dumps(_snapshot_brief(snap), ensure_ascii=False),
        },
    )
    pid = int(row.lastrowid or 0)
    db.commit()
    proposal = get_proposal(db, pid)

    if not risk.ok:
        logger.info(f"[提案] #{pid} {intent.inst_id} 创建即风控拦截: {[v['code'] for v in risk.violations]}")
        return ProposalOutcome(proposal, risk_dict, ok=False, message="风控校验未通过,禁止下单")

    logger.info(f"[提案] #{pid} {intent.inst_id} {probe.ord_type} {intent.side} sz={intent.sz} 待人工确认")
    return ProposalOutcome(proposal, risk_dict, ok=True, message="待人工确认")


def confirm_proposal(
    db: Session, pid: int, *,
    account_svc: AccountQueryService, algo_svc: AlgoOrderService, cfg: RiskConfig,
) -> ProposalOutcome:
    """人工确认:重拉账户 → 重跑风控 → 通过才下单。

    任何失败(状态不对/风控不过/OKX 错误)都不会发起下单,状态落 rejected/risk_failed/failed。
    """
    p = get_proposal(db, pid)
    if not p:
        raise OKXAgentError("NOT_FOUND", "提案不存在")
    if p["status"] != "pending_confirm":
        raise OKXAgentError("INVALID_STATE", f"提案状态为 {p['status']},仅 pending_confirm 可确认")

    if not cfg.enabled:
        _update_status(db, pid, "risk_failed", s_code="RISK_DISABLED", s_msg="风控模块已禁用")
        raise OKXAgentError("RISK_DISABLED", "策略委托模块已禁用,无法确认")

    # ---- 二次风控:用此刻的账户状态 ----
    intent = _intent_from_proposal(p)
    snap = account_svc.snapshot(p["inst_id"], td_mode=p["td_mode"])
    risk = RiskChecker(cfg).validate(snap, intent)
    risk_dict = risk.to_dict()

    if not risk.ok:
        _update_status(
            db, pid, "risk_failed",
            risk_check=json.dumps(risk_dict, ensure_ascii=False),
            snapshot_after=json.dumps(_snapshot_brief(snap), ensure_ascii=False),
            s_code=risk.violations[0]["code"] if risk.violations else "RISK_FAIL",
            s_msg="; ".join(v["msg"] for v in risk.violations)[:500],
        )
        logger.info(f"[提案] #{pid} 确认时风控拦截: {[v['code'] for v in risk.violations]}")
        return ProposalOutcome(get_proposal(db, pid), risk_dict, ok=False, message="确认时风控校验未通过,未发起下单")

    # ---- 风控通过 → 下单 ----
    req = _algo_req_from_proposal(p)
    # OKX 要求:字母开头,仅字母数字,≤32 位
    req.cl_ord_id = f"algoP{pid}{int(__import__('time').time())}"[:32]
    req.pos_mode = snap.pos_mode  # 双向模式需 posSide,单向模式禁传
    logger.info(f"[提案] #{pid} 用户已确认,风控通过,提交策略委托 {p['inst_id']} {p['ord_type']}")
    try:
        rec = algo_svc.place_algo_order(req, db)
    except OKXAgentError as e:
        _update_status(
            db, pid, "failed", s_code=e.code, s_msg=e.msg,
            risk_check=json.dumps(risk_dict, ensure_ascii=False),
            snapshot_after=json.dumps(_snapshot_brief(snap), ensure_ascii=False),
        )
        return ProposalOutcome(get_proposal(db, pid), risk_dict, ok=False, message=f"OKX 下单失败: {e.msg}")

    _update_status(
        db, pid, "confirmed", algo_id=rec.algo_id, s_code=rec.s_code, s_msg=rec.s_msg,
        risk_check=json.dumps(risk_dict, ensure_ascii=False),
        snapshot_after=json.dumps(_snapshot_brief(snap), ensure_ascii=False),
    )
    logger.info(f"[提案] #{pid} 策略委托已提交 algoId={rec.algo_id}")
    return ProposalOutcome(get_proposal(db, pid), risk_dict, ok=True, message=f"已提交 algoId={rec.algo_id}")


def reject_proposal(db: Session, pid: int) -> dict:
    p = get_proposal(db, pid)
    if not p:
        raise OKXAgentError("NOT_FOUND", "提案不存在")
    if p["status"] != "pending_confirm":
        raise OKXAgentError("INVALID_STATE", f"提案状态为 {p['status']},仅 pending_confirm 可拒绝")
    _update_status(db, pid, "rejected")
    logger.info(f"[提案] #{pid} 用户拒绝")
    return get_proposal(db, pid)
