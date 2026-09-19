"""OKX Agent 策略(TradingAgents 决策 → 人工确认执行)单元测试。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.modules.okx_agent import strategy as ta_strategy


@pytest.fixture()
def db(tmp_path):
    """独立 SQLite 库,建 ta_trade_strategies 表。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    with engine.begin() as conn:
        conn.execute(text("""
CREATE TABLE ta_trade_strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inst_id TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'hold',
    action_label TEXT NOT NULL DEFAULT '',
    rating_raw TEXT NOT NULL DEFAULT '',
    confidence REAL DEFAULT 0,
    ord_type TEXT NOT NULL DEFAULT 'market',
    td_mode TEXT NOT NULL DEFAULT 'cash',
    sz TEXT NOT NULL DEFAULT '',
    px TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    analysis_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    ord_id TEXT,
    okx_response TEXT,
    error_msg TEXT NOT NULL DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""))
    yield sessionmaker(bind=engine)()
    engine.dispose()


ANALYSIS_BUY = {
    "raw_data": {
        "suggestion": {
            "action": "buy",
            "action_label": "买入",
            "rating_raw": "buy",
            "confidence": 7.5,
            "reason": "多重利好共振,建议建仓",
        }
    },
    "analysis_date": "2026-09-19",
    "trace_id": "okx-ta-BTC-USDT-123",
}

ANALYSIS_HOLD = {
    "raw_data": {"suggestion": {"action": "hold", "action_label": "持有", "confidence": 5}},
    "analysis_date": "2026-09-19",
}


class TestCreateStrategy:
    def test_buy_creates_pending(self, db: Session):
        rec = ta_strategy.create_strategy_from_analysis(db, "BTC-USDT", ANALYSIS_BUY)
        assert rec is not None
        assert rec["inst_id"] == "BTC-USDT"
        assert rec["action"] == "buy"
        assert rec["action_label"] == "买入"
        assert rec["confidence"] == pytest.approx(7.5)
        assert rec["status"] == "pending"
        assert rec["ord_type"] == "market"
        assert rec["trace_id"] == "okx-ta-BTC-USDT-123"

    def test_hold_no_strategy(self, db: Session):
        assert ta_strategy.create_strategy_from_analysis(db, "BTC-USDT", ANALYSIS_HOLD) is None
        assert ta_strategy.list_strategies(db) == []

    def test_empty_suggestion_no_strategy(self, db: Session):
        assert ta_strategy.create_strategy_from_analysis(db, "BTC-USDT", {}) is None


class TestLifecycle:
    def test_update_status(self, db: Session):
        rec = ta_strategy.create_strategy_from_analysis(db, "ETH-USDT", ANALYSIS_BUY)
        sid = rec["id"]
        ta_strategy.update_strategy_status(db, sid, "executed", ord_id="123", okx_response='{"sCode":"0"}')
        got = ta_strategy.get_strategy(db, sid)
        assert got["status"] == "executed"
        assert got["ord_id"] == "123"

    def test_expire_stale(self, db: Session):
        rec = ta_strategy.create_strategy_from_analysis(db, "BTC-USDT", ANALYSIS_BUY)
        # 手动把 created_at 拨回 25h 前
        db.execute(text(
            "UPDATE ta_trade_strategies SET created_at = DATETIME('now', '-25 hours') WHERE id = :i"
        ), {"i": rec["id"]})
        db.commit()
        out = ta_strategy.list_strategies(db)
        assert out[0]["status"] == "expired"
        # expired 不可再审批(approve 端点会拒)

    def test_fresh_not_expired(self, db: Session):
        ta_strategy.create_strategy_from_analysis(db, "BTC-USDT", ANALYSIS_BUY)
        out = ta_strategy.list_strategies(db, status="pending")
        assert len(out) == 1


class TestAnalysisThreads:
    def test_spawn_and_done(self):
        import time
        ran = []
        ta_strategy.spawn_analysis("TEST-USDT", lambda: ran.append(1))
        time.sleep(0.2)
        assert ran == [1]
        assert not ta_strategy.is_analyzing("TEST-USDT")

    def test_error_not_propagate(self):
        def boom():
            raise RuntimeError("x")
        ta_strategy.spawn_analysis("TEST2-USDT", boom)
        import time
        time.sleep(0.2)
        assert not ta_strategy.is_analyzing("TEST2-USDT")
