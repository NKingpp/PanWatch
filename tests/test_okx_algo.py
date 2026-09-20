"""OKX 策略委托模块单测:风控校验 / payload 构造 / 提案流程(mock,不发真实请求)。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from src.modules.okx_agent.account import AccountSnapshot, InstrumentSpec
from src.modules.okx_agent.algo import AlgoOrderRequest
from src.modules.okx_agent.client import OKXAgentError
from src.modules.okx_agent.risk import RiskChecker, RiskConfig, TradeIntent
from src.modules.okx_agent import proposal as P


# ---------- 夹具 ----------

def _spec(inst_type="SPOT") -> InstrumentSpec:
    """SPOT:币数量,无步长;SWAP:张数(测试里 minSz=1, lotSz=1)。"""
    if inst_type == "SPOT":
        return InstrumentSpec(inst_id="BTC-USDT", inst_type="SPOT", ct_val=1.0, lot_sz=0, min_sz=0)
    return InstrumentSpec(inst_id="BTC-USDT-SWAP", inst_type=inst_type, ct_val=0.01, lot_sz=1, min_sz=1)


def _snap(**kw) -> AccountSnapshot:
    defaults = dict(
        total_eq_usd=10000.0,
        available_ccy={"USDT": 5000.0, "BTC": 0.1},
        imr=0.1, mmr=0.02, mgn_ratio="",
        lever="5", last_px=50000.0,
    )
    snap = AccountSnapshot(**{**defaults, **kw})
    snap.spec = kw.get("spec") or _spec()
    return snap


def _spot_intent(sz="0.05", side="buy", **kw) -> TradeIntent:
    return TradeIntent(inst_id="BTC-USDT", side=side, td_mode="cash", sz=sz, **kw)


# ---------- 风控:现货 ----------

def test_risk_spot_pass():
    r = RiskChecker(RiskConfig()).validate(_snap(), _spot_intent("0.05"))  # 0.05*50000=2500
    assert r.ok and r.violations == []
    assert r.notional_usd == pytest.approx(2500.0, rel=1e-6)

def test_risk_spot_insufficient_balance():
    r = RiskChecker(RiskConfig()).validate(_snap(available_ccy={"USDT": 100.0}), _spot_intent("0.05"))
    assert not r.ok
    assert any(v["code"] == "INSUFFICIENT_BALANCE" for v in r.violations)

def test_risk_spot_sell_no_balance_needed():
    """现货卖出不需要新资金,即使可用余额低也放行。"""
    r = RiskChecker(RiskConfig()).validate(_snap(available_ccy={}), _spot_intent("0.05", side="sell"))
    assert r.ok

def test_risk_spot_notional_cap():
    cfg = RiskConfig(max_notional_usd=1000.0)
    r = RiskChecker(cfg).validate(_snap(), _spot_intent("0.05"))  # 2500 > 1000
    assert not r.ok and any(v["code"] == "NOTIONAL_CAP" for v in r.violations)

def test_risk_spot_notional_pct():
    cfg = RiskConfig(max_notional_pct=20.0)
    r = RiskChecker(cfg).validate(_snap(), _spot_intent("0.05"))  # 2500/10000=25%
    assert not r.ok and any(v["code"] == "NOTIONAL_PCT" for v in r.violations)

def test_risk_net_too_low():
    r = RiskChecker(RiskConfig()).validate(_snap(total_eq_usd=5.0), _spot_intent("0.001"))
    assert not r.ok and any(v["code"] == "NET_TOO_LOW" for v in r.violations)

def test_risk_disabled_blocks_all():
    r = RiskChecker(RiskConfig(enabled=False)).validate(_snap(), _spot_intent("0.001"))
    assert not r.ok and r.violations[0]["code"] == "RISK_DISABLED"

def test_risk_invalid_sz_rejected():
    r = RiskChecker(RiskConfig()).validate(_snap(), _spot_intent("abc"))
    assert not r.ok and any(v["code"] == "INVALID_SZ" for v in r.violations)

def test_risk_no_price():
    r = RiskChecker(RiskConfig()).validate(_snap(last_px=0.0), _spot_intent("0.01"))
    assert not r.ok and any(v["code"] == "NO_PRICE" for v in r.violations)

def test_risk_sz_not_lot():
    r = RiskChecker(RiskConfig()).validate(_snap(), _spot_intent("0.0555"))  # lotSz=1 张数场景
    # SPOT lotSz=1 对币数量不适用(币数量小数);SWAP 场景才拦截 — SPOT ct_val=0.01 时 sz 是币,不应拦
    # 这里断言 SPOT 不因步长拦截(小数币数量合法)
    assert r.ok

# ---------- 风控:合约 ----------

def _swap_snap(**kw) -> AccountSnapshot:
    s = _snap(**{k: v for k, v in kw.items() if k != "spec"})
    s.spec = InstrumentSpec(inst_id="BTC-USDT-SWAP", inst_type="SWAP", ct_val=0.01, lot_sz=1, min_sz=1)
    return s

def _swap_intent(sz="2", side="buy", td_mode="cross", **kw) -> TradeIntent:
    """2 张 × 0.01 BTC × 50000 = 1000 USD 名义。"""
    return TradeIntent(inst_id="BTC-USDT-SWAP", side=side, td_mode=td_mode, sz=sz, **kw)

def test_risk_swap_margin_ok():
    r = RiskChecker(RiskConfig()).validate(_swap_snap(), _swap_intent("2"))  # 1000/5=200 margin
    assert r.ok, r.violations
    assert r.est_margin_usd == pytest.approx(200.0, rel=1e-6)

def test_risk_swap_margin_pct():
    cfg = RiskConfig(max_margin_pct=1.0)  # 200/10000=2% > 1%
    r = RiskChecker(cfg).validate(_swap_snap(), _swap_intent("2"))
    assert not r.ok and any(v["code"] == "MARGIN_PCT" for v in r.violations)

def test_risk_swap_imr_hard_limit():
    r = RiskChecker(RiskConfig()).validate(_swap_snap(imr=0.85), _swap_intent("2"))
    assert not r.ok and any(v["code"] == "IMR_TOO_HIGH" for v in r.violations)

def test_risk_swap_insufficient_margin():
    r = RiskChecker(RiskConfig()).validate(
        _swap_snap(available_ccy={"USDT": 100.0}), _swap_intent("2"))
    assert not r.ok and any(v["code"] == "INSUFFICIENT_MARGIN" for v in r.violations)

def test_risk_swap_reduce_only_skips_margin():
    """reduceOnly 平仓不校验保证金/新资金,但名义上限仍生效。"""
    r = RiskChecker(RiskConfig()).validate(
        _swap_snap(available_ccy={}), _swap_intent("2", reduce_only=True))
    assert r.ok, r.violations

def test_risk_swap_lot_step():
    """张数 2.5 非整数步长 → SZ_NOT_LOT。"""
    s = _swap_snap()
    s.spec = InstrumentSpec(inst_id="BTC-USDT-SWAP", inst_type="SWAP", ct_val=0.01, lot_sz=1, min_sz=1)
    r = RiskChecker(RiskConfig()).validate(s, _swap_intent("2.5"))
    assert not r.ok and any(v["code"] == "SZ_NOT_LOT" for v in r.violations)

def test_risk_swap_min_sz():
    s = _swap_snap()
    s.spec = InstrumentSpec(inst_id="BTC-USDT-SWAP", inst_type="SWAP", ct_val=0.01, lot_sz=1, min_sz=2)
    r = RiskChecker(RiskConfig()).validate(s, _swap_intent("1"))
    assert not r.ok and any(v["code"] == "SZ_TOO_SMALL" for v in r.violations)

# ---------- AlgoOrderRequest payload ----------

def test_algo_req_conditional_body():
    req = AlgoOrderRequest(
        inst_id="BTC-USDT-SWAP", td_mode="cross", side="sell", ord_type="conditional",
        sz="2", tp_trigger_px="55000", sl_trigger_px="48000", reduce_only=True,
    )
    req.validate()
    body = req.to_okx_body()
    assert body["ordType"] == "conditional" and body["reduceOnly"] is True
    assert body["tpTriggerPx"] == "55000" and body["tpOrdPx"] == "-1"
    assert body["slTriggerPx"] == "48000"
    assert "triggerPx" not in body  # conditional 不用 triggerPx

def test_algo_req_trigger_body():
    req = AlgoOrderRequest(
        inst_id="BTC-USDT", td_mode="cash", side="buy", ord_type="trigger",
        sz="0.01", trigger_px="45000", order_px="-1",
    )
    req.validate()
    body = req.to_okx_body()
    assert body["triggerPx"] == "45000" and body["orderPx"] == "-1"
    assert "tpTriggerPx" not in body

def test_algo_req_move_body():
    req = AlgoOrderRequest(
        inst_id="BTC-USDT-SWAP", td_mode="cross", side="sell", ord_type="move",
        sz="2", callback_ratio="0.05", move_trigger_px="52000",
    )
    req.validate()
    body = req.to_okx_body()
    assert body["callbackRatio"] == "0.05" and body["moveTriggerPx"] == "52000"

def test_algo_req_oco_needs_tp_or_sl():
    req = AlgoOrderRequest(
        inst_id="BTC-USDT", td_mode="cash", side="buy", ord_type="oco", sz="0.01",
    )
    with pytest.raises(OKXAgentError, match="oco"):
        req.validate()

def test_algo_req_missing_trigger_px():
    req = AlgoOrderRequest(
        inst_id="BTC-USDT", td_mode="cash", side="buy", ord_type="trigger", sz="0.01",
    )
    with pytest.raises(OKXAgentError, match="触发价"):
        req.validate()

def test_algo_req_bad_ord_type():
    req = AlgoOrderRequest(
        inst_id="BTC-USDT", td_mode="cash", side="buy", ord_type="market", sz="0.01",
    )
    with pytest.raises(OKXAgentError, match="ord_type"):
        req.validate()

# ---------- 提案流程(mock 快照与下单) ----------

class _FakeAccountSvc:
    def __init__(self, snap): self.snap = snap
    def snapshot(self, inst_id, *, td_mode): return self.snap

class _FakeAlgoSvc:
    def __init__(self): self.placed = None
    def place_algo_order(self, req, db):
        self.placed = req
        from src.modules.okx_agent.algo import AlgoExecutionRecord
        return AlgoExecutionRecord(algo_id="A123", s_code="0", s_msg="ok")

def _db_session(tmp_db_engine):
    from sqlalchemy.orm import Session
    return Session(tmp_db_engine)

@pytest.fixture
def tmp_db_engine(tmp_path):
    from sqlalchemy import create_engine
    # okx_algo_proposals 表(与迁移一致的最小 schema)
    eng = create_engine(f"sqlite:///{tmp_path / 'algo.db'}")
    with eng.begin() as c:
        c.execute(text("""
CREATE TABLE okx_algo_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id TEXT NOT NULL, td_mode TEXT NOT NULL DEFAULT 'cash',
    side TEXT NOT NULL, ord_type TEXT NOT NULL, sz TEXT NOT NULL DEFAULT '',
    order_px TEXT NOT NULL DEFAULT '', tp_trigger_px TEXT NOT NULL DEFAULT '',
    tp_ord_px TEXT NOT NULL DEFAULT '-1', sl_trigger_px TEXT NOT NULL DEFAULT '',
    sl_ord_px TEXT NOT NULL DEFAULT '-1', trigger_px TEXT NOT NULL DEFAULT '',
    callback_ratio TEXT NOT NULL DEFAULT '', move_trigger_px TEXT NOT NULL DEFAULT '',
    reduce_only INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending_confirm',
    algo_id TEXT, s_code TEXT NOT NULL DEFAULT '', s_msg TEXT NOT NULL DEFAULT '',
    risk_check TEXT, snapshot_before TEXT, snapshot_after TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""))
    return eng

def test_create_proposal_pass_then_confirm(tmp_db_engine):
    db = _db_session(tmp_db_engine)
    body = {"inst_id": "BTC-USDT", "side": "buy", "td_mode": "cash", "ord_type": "conditional",
            "sz": "0.05", "tp_trigger_px": "55000"}
    out = P.create_proposal(db, body, account_svc=_FakeAccountSvc(_snap()), cfg=RiskConfig())
    assert out.ok and out.proposal["status"] == "pending_confirm"
    assert json.loads(out.proposal["risk_check"])["ok"] is True

    # 确认:资金没变 → 通过 → 调下单
    algo = _FakeAlgoSvc()
    out2 = P.confirm_proposal(db, out.proposal["id"], account_svc=_FakeAccountSvc(_snap()), algo_svc=algo, cfg=RiskConfig())
    assert out2.ok and out2.proposal["status"] == "confirmed"
    cid = algo.placed.cl_ord_id
    # OKX 规则:字母开头、仅字母数字、≤32 位
    assert cid and cid[0].isalpha() and cid.isalnum() and len(cid) <= 32 and cid.startswith("algoP")

def test_create_proposal_risk_fail(tmp_db_engine):
    db = _db_session(tmp_db_engine)
    body = {"inst_id": "BTC-USDT", "side": "buy", "td_mode": "cash", "ord_type": "conditional",
            "sz": "5", "tp_trigger_px": "55000"}  # 5*50000=250000 超单笔上限
    out = P.create_proposal(db, body, account_svc=_FakeAccountSvc(_snap()), cfg=RiskConfig())
    assert not out.ok and out.proposal["status"] == "risk_failed"
    assert any(v["code"] == "NOTIONAL_CAP" for v in out.risk["violations"])

def test_confirm_rechecks_and_blocks(tmp_db_engine):
    """确认时账户余额变了 → 二次风控拦截,不下单。"""
    db = _db_session(tmp_db_engine)
    body = {"inst_id": "BTC-USDT", "side": "buy", "td_mode": "cash", "ord_type": "conditional",
            "sz": "0.05", "tp_trigger_px": "55000"}
    out = P.create_proposal(db, body, account_svc=_FakeAccountSvc(_snap()), cfg=RiskConfig())
    algo = _FakeAlgoSvc()
    # 确认时 USDT 被花光
    broke = _snap(available_ccy={"USDT": 10.0})
    out2 = P.confirm_proposal(db, out.proposal["id"], account_svc=_FakeAccountSvc(broke), algo_svc=algo, cfg=RiskConfig())
    assert not out2.ok
    assert out2.proposal["status"] == "risk_failed"
    assert algo.placed is None  # 没有发起下单

def test_reject_flow(tmp_db_engine):
    db = _db_session(tmp_db_engine)
    body = {"inst_id": "BTC-USDT", "side": "buy", "td_mode": "cash", "ord_type": "conditional",
            "sz": "0.05", "tp_trigger_px": "55000"}
    out = P.create_proposal(db, body, account_svc=_FakeAccountSvc(_snap()), cfg=RiskConfig())
    p = P.reject_proposal(db, out.proposal["id"])
    assert p["status"] == "rejected"

def test_confirm_wrong_state_rejected(tmp_db_engine):
    db = _db_session(tmp_db_engine)
    body = {"inst_id": "BTC-USDT", "side": "buy", "td_mode": "cash", "ord_type": "conditional",
            "sz": "0.05", "tp_trigger_px": "55000"}
    out = P.create_proposal(db, body, account_svc=_FakeAccountSvc(_snap()), cfg=RiskConfig())
    P.reject_proposal(db, out.proposal["id"])
    algo = _FakeAlgoSvc()
    with pytest.raises(OKXAgentError, match="INVALID_STATE"):
        P.confirm_proposal(db, out.proposal["id"], account_svc=_FakeAccountSvc(_snap()), algo_svc=algo, cfg=RiskConfig())

def test_proposal_ttl_expiry(tmp_db_engine):
    db = _db_session(tmp_db_engine)
    body = {"inst_id": "BTC-USDT", "side": "buy", "td_mode": "cash", "ord_type": "conditional",
            "sz": "0.05", "tp_trigger_px": "55000"}
    out = P.create_proposal(db, body, account_svc=_FakeAccountSvc(_snap()), cfg=RiskConfig())
    # 人为把 created_at 拨到 25h 前
    db.execute(text("UPDATE okx_algo_proposals SET created_at = DATETIME('now', '-25 hours') WHERE id = :i"),
               {"i": out.proposal["id"]})
    db.commit()
    items = P.list_proposals(db)
    assert items[0]["status"] == "expired"
