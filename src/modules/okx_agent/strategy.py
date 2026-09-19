"""TradingAgents 深度决策 → OKX 交易策略(待人工确认)。

流程:用户选交易对 → 触发 TradingAgents 多 Agent 深度分析(后台 3-5 分钟)
→ 完成后从 AnalysisHistory 读 raw_data.suggestion → 生成 pending 策略
→ 用户审批:approve = 调 OKXAgentService 下单, reject = 丢弃。
"""
from __future__ import annotations

import json
import logging
import threading

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 策略有效期:超时未审批自动 expired(小时)
STRATEGY_TTL_HOURS = 24


def _load_raw_data(analysis: dict) -> dict:
    """raw_data 可能是 dict(ORM JSON)或 str(手工插入),统一成 dict。"""
    raw = analysis.get("raw_data") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    return raw if isinstance(raw, dict) else {}


def create_strategy_from_analysis(db: Session, inst_id: str, analysis: dict) -> dict | None:
    """从 TradingAgents AnalysisHistory 记录生成 pending 策略。

    analysis: /api/agents/tradingagents/latest 返回结构(含 raw_data)。
    hold / review 评级不生成可执行策略,返回 None。
    """
    raw = _load_raw_data(analysis)
    sug = raw.get("suggestion") or {}
    action = (sug.get("action") or "").lower()
    if action not in ("buy", "sell"):
        return None

    # 数量留空,审批时用户填(或用默认仓位规则);价格留空 = 市价单
    result = db.execute(
        text("""
INSERT INTO ta_trade_strategies
    (inst_id, action, action_label, rating_raw, confidence, ord_type, td_mode,
     sz, px, reason, trace_id, analysis_date, status)
VALUES (:iid, :act, :alabel, :rating, :conf, 'market', 'cash', '', '',
        :reason, :trace, :adate, 'pending')
"""),
        {
            "iid": inst_id,
            "act": action,
            "alabel": sug.get("action_label") or "",
            "rating": sug.get("rating_raw") or "",
            "conf": float(sug.get("confidence") or 0),
            "reason": (sug.get("reason") or "")[:2000],
            "trace": analysis.get("trace_id") or "",
            "adate": analysis.get("analysis_date") or "",
        },
    )
    sid = int(result.lastrowid or 0)
    db.commit()
    return get_strategy(db, sid) if sid else None


_COLS = (
    "id, inst_id, action, action_label, rating_raw, confidence, ord_type, td_mode, "
    "sz, px, reason, trace_id, analysis_date, status, ord_id, error_msg, created_at, updated_at"
)


def get_strategy(db: Session, strategy_id: int) -> dict | None:
    rows = db.execute(
        text(f"SELECT {_COLS} FROM ta_trade_strategies WHERE id = :i"), {"i": strategy_id}
    ).fetchall()
    cols = [c.strip() for c in _COLS.split(",")]
    return dict(zip(cols, rows[0])) if rows else None


def list_strategies(db: Session, status: str = "", inst_id: str = "", limit: int = 50) -> list[dict]:
    """查策略列表,默认按新→旧。同时把过期 pending 标记 expired。"""
    _expire_stale(db)
    sql = f"SELECT {_COLS} FROM ta_trade_strategies"
    conds, params = [], {"lim": min(max(limit, 1), 200)}
    if status:
        conds.append("status = :st")
        params["st"] = status
    if inst_id:
        conds.append("inst_id = :iid")
        params["iid"] = inst_id
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT :lim"
    rows = db.execute(text(sql), params).fetchall()
    cols = [c.strip() for c in _COLS.split(",")]
    return [dict(zip(cols, r)) for r in rows]


def _expire_stale(db: Session) -> None:
    """pending 策略超过 TTL 自动 expired,防止老策略被误执行。"""
    db.execute(
        text("""
UPDATE ta_trade_strategies
SET status = 'expired', updated_at = CURRENT_TIMESTAMP
WHERE status = 'pending'
  AND created_at < DATETIME('now', :neg_hours || ' hours')
"""),
        {"neg_hours": -STRATEGY_TTL_HOURS},
    )
    db.commit()


def update_strategy_status(
    db: Session, strategy_id: int, status: str,
    ord_id: str = "", error_msg: str = "", okx_response: str = "",
) -> None:
    db.execute(
        text("""
UPDATE ta_trade_strategies
SET status = :st, ord_id = :oid, error_msg = :em, okx_response = :resp,
    updated_at = CURRENT_TIMESTAMP
WHERE id = :i
"""),
        {"st": status, "oid": ord_id or None, "em": error_msg[:1000],
         "resp": okx_response[:4000], "i": strategy_id},
    )
    db.commit()


# ---------- 后台分析线程管理:同 inst_id 不重复触发 ----------

_analyze_threads: dict[str, threading.Thread] = {}
_analyze_lock = threading.Lock()


def is_analyzing(inst_id: str) -> bool:
    with _analyze_lock:
        t = _analyze_threads.get(inst_id)
        return bool(t and t.is_alive())


def spawn_analysis(inst_id: str, runner) -> None:
    """启动后台分析线程,完成后自动清理。runner: 零参回调。"""
    def _wrap():
        try:
            runner()
        except Exception:
            logger.exception(f"[TA策略] {inst_id} 后台分析失败")
        finally:
            with _analyze_lock:
                _analyze_threads.pop(inst_id, None)

    with _analyze_lock:
        t = threading.Thread(target=_wrap, name=f"ta-strategy-{inst_id}", daemon=True)
        _analyze_threads[inst_id] = t
        t.start()
