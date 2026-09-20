"""OKX Agent 策略(TradingAgents 决策 → 人工确认执行)单元测试。"""
from __future__ import annotations

import sqlite3
import time
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
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    price_at_analysis REAL,
    model_label TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER
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

    def test_hold_records_skip(self, db: Session):
        """hold 决策也落库为 skip 记录(历史可查,不可执行)。"""
        rec = ta_strategy.create_strategy_from_analysis(db, "BTC-USDT", ANALYSIS_HOLD)
        assert rec is not None
        assert rec["action"] == "hold"
        assert rec["action_label"] == "持有"
        assert rec["status"] == "skip"

    def test_hold_not_in_pending_flow(self, db: Session):
        """skip 记录不进入 pending 审批流(list 带 status 过滤)。"""
        ta_strategy.create_strategy_from_analysis(db, "BTC-USDT", ANALYSIS_HOLD)
        assert ta_strategy.list_strategies(db, status="pending") == []
        assert len(ta_strategy.list_strategies(db)) == 1

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


class TestAutoSizing:
    """api._auto_size_position 的纯函数部分:仓位比例映射 + lotSz 对齐。"""

    def test_conf_to_pct(self):
        from src.modules.okx_agent.api import _conf_to_pct
        assert _conf_to_pct(9.0) == 0.30
        assert _conf_to_pct(8.0) == 0.30
        assert _conf_to_pct(6.5) == 0.20
        assert _conf_to_pct(4.0) == 0.10
        assert _conf_to_pct(1.0) == 0.05

    def test_align_sz_btc(self):
        # BTC-USDT: lotSz=0.00000001 但常规 0.00001 步长,minSz=0.00001
        from src.modules.okx_agent.api import _align_sz
        assert _align_sz(0.0012345, 0.00001, 0.00001) == "0.00123"
        assert _align_sz(0.000004, 0.00001, 0.00001) == ""   # 低于 minSz
        assert _align_sz(0, 0.00001, 0.00001) == ""

    def test_align_sz_eth(self):
        # ETH-USDT: lotSz=0.001, minSz=0.001
        from src.modules.okx_agent.api import _align_sz
        assert _align_sz(0.12345, 0.001, 0.001) == "0.123"
        assert _align_sz(1.7, 0.1, 0.1) == "1.7"

    def test_align_sz_no_step(self):
        from src.modules.okx_agent.api import _align_sz
        assert _align_sz(3.14159, 0, 0) == "3.14159"
        assert _align_sz(2.0, 0, 3.0) == ""   # 无步长但低于 minSz


class TestProgressStreamEvents:
    """PanWatchProgressHandler 的角色思考文本输出(token + 全量)。"""

    def _handler(self):
        from src.modules.automation.tradingagents.progress import PanWatchProgressHandler
        return PanWatchProgressHandler("trace-x", "tradingagents")

    def test_llm_end_emits_text(self):
        from types import SimpleNamespace
        h = self._handler()
        h.on_chain_start(None, {}, name="Market Analyst")
        gen = SimpleNamespace(text="市场情绪偏多,资金流入明显")
        resp = SimpleNamespace(generations=[[gen]], llm_output={})
        h.on_llm_end(resp)
        assert h._current_stage == "market_analyst"

    def test_llm_new_token_buffers(self):
        h = self._handler()
        h.on_llm_new_token("a" * 40)
        h.on_llm_new_token("b" * 60)   # 超过 80 字符 → 自动 flush
        assert len(h._stream_buffer) == 0
        h.on_llm_new_token("c")
        assert len(h._stream_buffer) == 1

    def test_normalize_stage_subroles(self):
        from src.modules.automation.tradingagents.progress import _normalize_stage
        assert _normalize_stage("Bull Researcher") == "bull_researcher"
        assert _normalize_stage("Bear Researcher") == "bear_researcher"
        assert _normalize_stage("Sentiment Analyst") == "social_analyst"
        assert _normalize_stage("Portfolio Manager") == "final_decision"
        assert _normalize_stage("Aggressive Analyst") == "aggressive_analyst"
        assert _normalize_stage("Research Manager") == "research_manager"
        assert _normalize_stage("tools_market") == ""

    def test_cancel_no_more_events(self):
        h = self._handler()
        h.on_chain_start(None, {}, name="Market Analyst")
        h.cancel()
        assert h.cancelled
        # 取消后所有回调 no-op(不发日志、不攒 buffer)
        h.on_llm_new_token("x" * 100)
        h._emit("market_analyst", "stage_end")
        h._emit_llm_text("llm_text", "不该出现")
        assert len(h._stream_buffer) == 0

    def test_llm_end_flushes_buffer_before_text(self):
        """on_llm_end 先 flush token 残余再发全量,防重复。"""
        from types import SimpleNamespace
        h = self._handler()
        h.on_chain_start(None, {}, name="News Analyst")
        h.on_llm_new_token("残余token")   # 不足 80 字符,滞留 buffer
        gen = SimpleNamespace(text="全量文本")
        resp = SimpleNamespace(generations=[[gen]], llm_output={})
        h.on_llm_end(resp)
        assert len(h._stream_buffer) == 0


class TestCancelAnalysis:
    """停止分析:协作式取消 + 死锁回归(非重入锁内不得再调 is_analyzing)。"""

    def test_cancel_no_analysis(self):
        assert ta_strategy.cancel_analysis("BTC-USDT") is False
        assert ta_strategy.is_cancelled("BTC-USDT") is False

    def test_cancel_while_running(self):
        import threading, time
        started = threading.Event()
        release = threading.Event()

        def runner():
            started.set()
            release.wait(timeout=5)

        ta_strategy.spawn_analysis("TEST-USDT", runner)
        try:
            assert started.wait(timeout=2)
            assert ta_strategy.is_analyzing("TEST-USDT")
            # 死锁回归:持锁路径上不得再抢锁(cancel_analysis 内联检查线程)
            assert ta_strategy.cancel_analysis("TEST-USDT") is True
            assert ta_strategy.is_cancelled("TEST-USDT")
        finally:
            release.set()

    def test_cancel_cleared_after_done(self):
        import threading
        release = threading.Event()
        ta_strategy.spawn_analysis("TEST2-USDT", release.wait)
        release.set()
        # 线程结束后清理取消标志
        for _ in range(50):
            if not ta_strategy.is_analyzing("TEST2-USDT"):
                break
            time.sleep(0.02)
        assert ta_strategy.is_cancelled("TEST2-USDT") is False
