"""OKX V5 行情 vendor 单测:解析/错误码/bar 映射/分页/限流透传,不打真实网络。"""

from __future__ import annotations

import pytest

import marketdata.vendors.okx as ov
from marketdata.symbol import Symbol
from marketdata.types import Bar, Quote


def _ticker_payload(**over):
    d = {
        "instType": "SPOT", "instId": "BTC-USDT", "last": "81069.7",
        "lastSz": "0.00592189", "askPx": "81069.7", "askSz": "0.71",
        "bidPx": "81069.6", "bidSz": "0.96",
        "open24h": "77900.1", "high24h": "81748", "low24h": "77659.5",
        "volCcy24h": "697383309.68", "vol24h": "8679.74",
        "ts": "1789805070372", "sodUtc0": "80903", "sodUtc8": "80728.8",
    }
    d.update(over)
    return {"code": "0", "data": [d], "msg": ""}


# ---------------- parse_ticker ----------------

def test_parse_ticker_full():
    t = ov.parse_ticker(_ticker_payload()["data"][0])
    assert t.inst_id == "BTC-USDT" and t.inst_type == "SPOT"
    assert t.last == 81069.7 and t.bid_px == 81069.6 and t.ask_px == 81069.7
    assert t.open24h == 77900.1 and t.vol24h == 8679.74
    assert t.ts is not None and t.ts.year == 2026


def test_parse_ticker_empty_fields_ok():
    """期权冷门合约 last/open24h 可能为空串 → None,change_pct 返回 None 不炸。"""
    t = ov.parse_ticker(
        _ticker_payload(last="", open24h="", high24h="", instType="OPTION")["data"][0]
    )
    assert t.last is None and t.open24h is None
    assert t.change_pct_24h is None


def test_change_pct_24h():
    t = ov.parse_ticker(_ticker_payload()["data"][0])
    assert t.change_pct_24h == pytest.approx((81069.7 - 77900.1) / 77900.1 * 100)


# ---------------- fetch_ticker / 错误处理 ----------------

def test_fetch_ticker_ok(monkeypatch):
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: _ticker_payload())
    t = ov.fetch_ticker("BTC-USDT")
    assert t.inst_id == "BTC-USDT"


def test_fetch_ticker_error_code_raises(monkeypatch):
    """code=51001(品种不存在)→ OKXApiError,不吞。"""
    monkeypatch.setattr(
        ov, "market_get",
        lambda *a, **k: {"code": "51001", "data": [], "msg": "Instrument ID doesn't exist."},
    )
    with pytest.raises(ov.OKXApiError) as ei:
        ov.fetch_ticker("BTC-USD-230929")
    assert ei.value.code == "51001"


def test_okx_get_network_failure_raises(monkeypatch):
    """market_get 网络失败返回 None → OKXApiError(code='-1')。"""
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: None)
    with pytest.raises(ov.OKXApiError) as ei:
        ov.fetch_ticker("BTC-USDT")
    assert ei.value.code == "-1"


def test_fetch_tickers_bad_inst_type(monkeypatch):
    monkeypatch.setattr(
        ov, "market_get",
        lambda *a, **k: {"code": "51000", "data": [], "msg": "Parameter instType error"},
    )
    with pytest.raises(ov.OKXApiError) as ei:
        ov.fetch_tickers("BAD")
    assert ei.value.code == "51000"


def test_fetch_tickers_batch(monkeypatch):
    p = _ticker_payload()
    p2 = _ticker_payload(instId="ETH-USDT", last="3000")
    monkeypatch.setattr(
        ov, "market_get", lambda *a, **k: {"code": "0", "data": p["data"] + p2["data"], "msg": ""}
    )
    out = ov.fetch_tickers("SPOT")
    assert len(out) == 2 and out[1].inst_id == "ETH-USDT"


# ---------------- candles ----------------

def _candle_row(ts, o, h, l, c, vol):
    return [str(ts), str(o), str(h), str(l), str(c), str(vol), "1", "1", "1"]


def test_fetch_candles_ascending_and_parse(monkeypatch):
    """OKX 返回降序(新→旧),输出必须升序(旧→新)。"""
    rows = [
        _candle_row(1789747200000, 80728.8, 81748, 80568.6, 81069.7, 4112.1),  # 最新
        _candle_row(1789660800000, 76780, 81155, 76258.2, 80728.7, 7853.3),
        _candle_row(1789574400000, 75790.5, 77167.3, 75055, 76780.1, 6913.9),  # 最旧
    ]
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: {"code": "0", "data": rows, "msg": ""})
    out = ov.fetch_candles("BTC-USDT", bar="day", limit=3)
    assert len(out) == 3 and isinstance(out[0], Bar)
    # OKX 日线 ts 锚 UTC16:00 (=UTC+8 零点): 09-17/18/19 三天,输出升序
    assert out[0].date == "2026-09-17" and out[2].date == "2026-09-19"
    assert out[0].open == 75790.5 and out[2].close == 81069.7
    assert out[1].volume == 7853.3


def test_fetch_candles_bar_map(monkeypatch):
    """timeframe day→1D、4h→4H、未知→1D。"""
    captured = {}

    def fake(url, *, params=None, **k):
        captured["params"] = params
        return {"code": "0", "data": [_candle_row(1789747200000, 1, 2, 0.5, 1.5, 10)], "msg": ""}

    monkeypatch.setattr(ov, "market_get", fake)
    ov.fetch_candles("BTC-USDT", bar="day", limit=1)
    assert captured["params"]["bar"] == "1D"
    ov.fetch_candles("BTC-USDT", bar="4h", limit=1)
    assert captured["params"]["bar"] == "4H"
    ov.fetch_candles("BTC-USDT", bar="whatever", limit=1)
    assert captured["params"]["bar"] == "1D"


def test_fetch_candles_minute_bar_keeps_time(monkeypatch):
    """非整日 K 线 date 保留 HH:MM。"""
    rows = [_candle_row(1789806000000, 1, 2, 0.5, 1.5, 10)]  # 非零点时间
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: {"code": "0", "data": rows, "msg": ""})
    out = ov.fetch_candles("BTC-USDT", bar="1h", limit=1)
    assert " " in out[0].date  # YYYY-MM-DD HH:MM


def test_fetch_candles_pagination(monkeypatch):
    """want > 300 时分页:第一页 100 条(after 请求携带游标),拼接后截断。"""
    page1 = [_candle_row(1780000000000 + i * 86400000, 1, 2, 0.5, 1.5, 10) for i in range(100)]
    page2 = [_candle_row(1770000000000 + i * 86400000, 1, 2, 0.5, 1.5, 10) for i in range(100)]
    calls = []

    def fake(url, *, params=None, **k):
        calls.append(params)
        return {"code": "0", "data": (page1 if len(calls) == 1 else page2), "msg": ""}

    monkeypatch.setattr(ov, "market_get", fake)
    out = ov.fetch_candles("BTC-USDT", bar="day", limit=150)
    assert len(calls) == 2
    assert calls[1].get("after") == str(page1[-1][0])  # 游标透传
    assert len(out) == 150


# ---------------- order book / trades ----------------

def test_fetch_order_book(monkeypatch):
    payload = {
        "code": "0", "msg": "",
        "data": [{
            "asks": [["81069.7", "0.80", "0", "11"], ["81070.9", "0.22", "0", "1"]],
            "bids": [["81069.6", "0.90", "0", "22"]],
            "ts": "1789805074509", "seqId": "81317251012",
        }],
    }
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: payload)
    book = ov.fetch_order_book("BTC-USDT", 5)
    assert book.asks == [(81069.7, 0.80, 0), (81070.9, 0.22, 0)]
    assert book.bids == [(81069.6, 0.90, 0)]
    assert book.seq_id == 81317251012


def test_fetch_order_book_empty_response_raises(monkeypatch):
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: {"code": "0", "data": [], "msg": ""})
    with pytest.raises(ov.OKXApiError):
        ov.fetch_order_book("BTC-USDT")


def test_fetch_trades(monkeypatch):
    payload = {
        "code": "0",
        "data": [
            {"instId": "BTC-USDT", "tradeId": "1059440449", "px": "81069.7",
             "sz": "0.00592189", "side": "buy", "ts": "1789805066559"},
        ],
        "msg": "",
    }
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: payload)
    trades = ov.fetch_trades("BTC-USDT", 3)
    assert len(trades) == 1
    assert trades[0].px == 81069.7 and trades[0].side == "buy"


# ---------------- instruments ----------------

def test_fetch_instruments_rejects_bad_type():
    with pytest.raises(ov.OKXApiError) as ei:
        ov.fetch_instruments("STOCK")
    assert ei.value.code == "51000"


def test_fetch_instruments_spot(monkeypatch):
    payload = {
        "code": "0",
        "data": [{
            "instId": "BTC-USDT", "instType": "SPOT", "baseCcy": "BTC", "quoteCcy": "USDT",
            "listTime": "1422124800000",
        }],
        "msg": "",
    }
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: payload)
    out = ov.fetch_instruments("SPOT")
    assert out[0].inst_id == "BTC-USDT" and out[0].base_ccy == "BTC"


# ---------------- Engine vendor 适配 ----------------

def test_okx_quote_vendor(monkeypatch):
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: _ticker_payload())
    vendor = ov.OKXQuoteVendor()
    assert vendor.name == "okx" and vendor.supports_markets == {"CRYPTO"}
    out = vendor.fetch([Symbol.parse("BTC-USDT", market="CRYPTO")], {})
    assert len(out) == 1 and isinstance(out[0], Quote)
    q = out[0]
    assert q.symbol == "BTC-USDT" and q.market == "CRYPTO"
    assert q.current_price == 81069.7
    assert q.change_pct == pytest.approx((81069.7 - 77900.1) / 77900.1 * 100)
    assert q.volume == 8679.74 and q.turnover == pytest.approx(697383309.68)


def test_okx_quote_vendor_error_swallows_per_symbol(monkeypatch):
    """单品种失败(如不存在的 instId)跳过,不拖垮整批。"""
    def fake(url, *, params=None, **k):
        if params.get("instId") == "BAD-PAIR":
            return {"code": "51001", "data": [], "msg": "doesn't exist"}
        return _ticker_payload()

    monkeypatch.setattr(ov, "market_get", fake)
    out = ov.OKXQuoteVendor().fetch(
        [Symbol.parse("BAD-PAIR", market="CRYPTO"), Symbol.parse("BTC-USDT", market="CRYPTO")],
        {},
    )
    assert len(out) == 1 and out[0].symbol == "BTC-USDT"


def test_okx_kline_vendor(monkeypatch):
    rows = [_candle_row(1789747200000, 80728.8, 81748, 80568.6, 81069.7, 4112.1)]
    monkeypatch.setattr(ov, "market_get", lambda *a, **k: {"code": "0", "data": rows, "msg": ""})
    out = ov.OKXKlineVendor().fetch(
        [Symbol.parse("BTC-USDT", market="CRYPTO")], {"days": 30}
    )
    assert len(out) == 1 and out[0].close == 81069.7


def test_okx_kline_vendor_bar_from_config(monkeypatch):
    captured = {}

    def fake(url, *, params=None, **k):
        captured["params"] = params
        return {"code": "0", "data": [_candle_row(1789806000000, 1, 2, 0.5, 1.5, 10)], "msg": ""}

    monkeypatch.setattr(ov, "market_get", fake)
    ov.OKXKlineVendor().fetch(
        [Symbol.parse("BTC-USDT", market="CRYPTO")], {"days": 10, "bar": "4h"}
    )
    assert captured["params"]["bar"] == "4H"


# ---------------- proxy 透传 ----------------

def test_proxy_threaded_to_market_get(monkeypatch):
    captured = {}

    def fake(url, *, proxy=None, **k):
        captured["proxy"] = proxy
        return _ticker_payload()

    monkeypatch.setattr(ov, "market_get", fake)
    ov.fetch_ticker("BTC-USDT", proxy="http://127.0.0.1:10808")
    assert captured["proxy"] == "http://127.0.0.1:10808"

    ov.fetch_ticker("BTC-USDT")
    assert captured["proxy"] is None  # 空串转 None,走 env 代理
