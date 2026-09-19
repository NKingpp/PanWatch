"""OKX V5 行情 vendor(现货/永续/交割/期权,只读行情,无签名无交易)。

公共行情接口(/api/v5/market/*、/api/v5/public/instruments)免鉴权 —— 按 OKX V5
文档,公共数据无需 APIKey 签名;本模块只实现行情读取,不做下单/划转。

限流: OKX 按接口独立限速(tickers 20 次/2s、ticker 50/2s、candles 40/2s、
books 40/2s、trades 100/2s、instruments 20/2s)。这里用 market_get 的
host_key 节流兜底(单 host 全局限 0.1s 间隔)+ 失败退避,足够覆盖当前用量。

错误处理: HTTP 异常/超时由 market_get 重试吸收;业务错误码(code != "0")
统一抛 OKXApiError,由 Engine 捕获后向备源转移。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from marketdata.http import market_get
from marketdata.types import Bar, OKXInstrument, OKXOrderBook, OKXTicker, OKXTrade

logger = logging.getLogger(__name__)

_BASE = "https://www.okx.com"
_HOST = "www.okx.com"
_MIN_INTERVAL_S = 0.1  # 10 req/s 全局兜底,低于各接口单独限速

# instType 枚举: 现货/永续合约/交割合约/期权
INST_TYPES = ("SPOT", "SWAP", "FUTURES", "OPTION")

# bar → OKX 周期参数。项目 timeframe(如 day/week)先经此映射。
_BAR_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H",
    "6h": "6H", "12h": "12H",
    "day": "1D", "1d": "1D",
    "week": "1W", "1w": "1W", "2w": "2W",
    "month": "1M", "1M": "1M",
    "1y": "1Y", "year": "1Y",
}


class OKXApiError(RuntimeError):
    """OKX 返回业务错误(code != "0")。message 形如 '51001: Instrument ID ... doesn't exist.'"""

    def __init__(self, code: str, msg: str, endpoint: str = ""):
        self.code = code
        self.endpoint = endpoint
        super().__init__(f"OKX {endpoint} code={code}: {msg}")


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_ts(v) -> datetime | None:
    """毫秒时间戳 → aware datetime(UTC)。"""
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def okx_get(path: str, params: dict, *, symbol: str = "", proxy: str = "") -> list:
    """GET /api/v5/* 公共接口。成功返回 data 列表;业务错误抛 OKXApiError;
    网络层失败(超时/5xx/解析失败)market_get 返回 None → 抛 OKXApiError(code="-1")。

    proxy: 显式代理(如 http://127.0.0.1:10808),来自数据源 config.proxy;空则走系统 env 代理。"""
    payload = market_get(
        _BASE + path,
        host_key=_HOST,
        params=params,
        min_interval_s=_MIN_INTERVAL_S,
        timeout=10,
        retries=2,
        parse="json",
        log_label="OKX行情",
        symbol=symbol,
        proxy=proxy or None,
    )
    if not isinstance(payload, dict):
        raise OKXApiError("-1", "network error or non-json response", path)
    code = str(payload.get("code", ""))
    if code != "0":
        msg = str(payload.get("msg", "") or payload.get("error", ""))
        logger.warning(f"OKX {path} params={params} 返回错误 code={code}: {msg}")
        raise OKXApiError(code, msg, path)
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    logger.debug(f"OKX {path} params={params} 返回 {len(data)} 条")
    return data


def parse_ticker(d: dict) -> OKXTicker:
    """单条 ticker/tickers 行 → OKXTicker。"""
    return OKXTicker(
        inst_id=d.get("instId", ""),
        inst_type=d.get("instType", ""),
        last=_to_float(d.get("last")),
        last_sz=_to_float(d.get("lastSz")),
        ask_px=_to_float(d.get("askPx")),
        ask_sz=_to_float(d.get("askSz")),
        bid_px=_to_float(d.get("bidPx")),
        bid_sz=_to_float(d.get("bidSz")),
        open24h=_to_float(d.get("open24h")),
        high24h=_to_float(d.get("high24h")),
        low24h=_to_float(d.get("low24h")),
        vol24h=_to_float(d.get("vol24h")),
        vol_ccy24h=_to_float(d.get("volCcy24h")),
        sod_utc0=_to_float(d.get("sodUtc0")),
        sod_utc8=_to_float(d.get("sodUtc8")),
        ts=_to_ts(d.get("ts")),
    )


def fetch_ticker(inst_id: str, *, proxy: str = "") -> OKXTicker:
    """单个品种最新行情(含最新成交价/买一卖一/24h 统计)。"""
    rows = okx_get("/api/v5/market/ticker", {"instId": inst_id}, symbol=inst_id, proxy=proxy)
    if not rows:
        raise OKXApiError("-1", f"empty ticker response for {inst_id}", "ticker")
    return parse_ticker(rows[0])


def fetch_tickers(inst_type: str, *, proxy: str = "") -> list[OKXTicker]:
    """全量行情快照(按品类: SPOT/SWAP/FUTURES/OPTION)。instType 非法抛 OKXApiError(51000)。"""
    rows = okx_get("/api/v5/market/tickers", {"instType": inst_type}, symbol=inst_type, proxy=proxy)
    return [parse_ticker(r) for r in rows if isinstance(r, dict)]


def fetch_order_book(inst_id: str, size: int = 20, *, proxy: str = "") -> OKXOrderBook:
    """盘口快照。size 1-400。"""
    size = min(max(int(size or 20), 1), 400)
    rows = okx_get(
        "/api/v5/market/books", {"instId": inst_id, "sz": str(size)}, symbol=inst_id, proxy=proxy
    )
    if not rows:
        raise OKXApiError("-1", f"empty book response for {inst_id}", "books")

    def _levels(raw) -> list[tuple[float, float, int]]:
        out = []
        for lv in raw or []:
            if isinstance(lv, (list, tuple)) and len(lv) >= 2:
                px, sz = _to_float(lv[0]), _to_float(lv[1])
                if px is not None and sz is not None:
                    out.append((px, sz, int(lv[2]) if len(lv) > 2 and str(lv[2]).isdigit() else 0))
        return out

    r0 = rows[0]
    return OKXOrderBook(
        inst_id=inst_id,
        asks=_levels(r0.get("asks")),
        bids=_levels(r0.get("bids")),
        ts=_to_ts(r0.get("ts")),
        seq_id=int(r0["seqId"]) if str(r0.get("seqId", "")).isdigit() else None,
    )


def fetch_trades(inst_id: str, limit: int = 100, *, proxy: str = "") -> list[OKXTrade]:
    """最新成交列表。limit 1-500,降序(最新在前)。"""
    limit = min(max(int(limit or 100), 1), 500)
    rows = okx_get(
        "/api/v5/market/trades", {"instId": inst_id, "limit": str(limit)}, symbol=inst_id, proxy=proxy
    )
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append(
            OKXTrade(
                inst_id=r.get("instId", inst_id),
                trade_id=str(r.get("tradeId", "")),
                px=_to_float(r.get("px")),
                sz=_to_float(r.get("sz")),
                side=str(r.get("side", "")),
                ts=_to_ts(r.get("ts")),
            )
        )
    return out


def _map_bar(bar: str) -> str:
    """项目 timeframe → OKX bar 参数;未知值当 1D。"""
    return _BAR_MAP.get(bar, "1D")


def fetch_candles(inst_id: str, *, bar: str = "day", limit: int = 120, proxy: str = "") -> list[Bar]:
    """K 线。OKX 返回降序(最新在前),统一转为升序输出(旧→新),对齐其他 kline vendor。

    limit 上限 300(历史 candles 接口 100);超出自动分页拉取后拼接。
    返回 Bar(date=YYYY-MM-DD HH:MM 交易时间, open/close/high/low, volume=币量)。
    """
    bar_param = _map_bar(bar)
    want = max(int(limit or 1), 1)
    out: list[Bar] = []
    after = ""  # 请求此时间戳之后(更早)的分页
    while len(out) < want:
        page_size = min(300, want - len(out))
        params = {"instId": inst_id, "bar": bar_param, "limit": str(max(page_size, 100))}
        if after:
            params["after"] = after
        rows = okx_get("/api/v5/market/candles", params, symbol=inst_id, proxy=proxy)
        if not rows:
            break
        page = _parse_candle_rows(rows, daily=bar_param in ("1D", "2D", "1W", "2W", "1M", "3M", "1Y"))
        if not page:
            break
        out = page + out  # 降序页拼接,保持升序
        if len(rows) < 100:  # 不足整页 → 已到头
            break
        after = str(rows[-1][0])  # 最后一行的 ts 作为 after 游标
    return out[-want:] if want else out


def _parse_candle_rows(rows: list, *, daily: bool = False) -> list[Bar]:
    """OKX candles 行 [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm] → list[Bar](升序)。

    daily=True(日/周/月线)date 只取日期;分钟/小时线保留到分钟。
    注意 OKX 日线时间戳锚 UTC+8 的 00:00(= UTC 16:00),不能按 UTC 零点判断。"""
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        ts = _to_ts(row[0])
        o, h, l, c = (_to_float(row[1]), _to_float(row[2]),
                      _to_float(row[3]), _to_float(row[4]))
        if ts is None or o is None or h is None or l is None or c is None:
            continue
        try:
            vol = _to_float(row[5]) or 0.0
        except Exception:
            vol = 0.0
        # ponytail: 日线按 UTC+8 取日期(OKX 日线口径);如有跨时区需求再引入 tz 配置
        fmt = "%Y-%m-%d" if daily else "%Y-%m-%d %H:%M"
        from datetime import timedelta
        local = ts + timedelta(hours=8) if daily else ts
        out.append(Bar(date=local.strftime(fmt), open=o, close=c, high=h, low=l, volume=vol))
    out.sort(key=lambda b: b.date)  # OKX 降序 → 升序
    return out


def fetch_instruments(
    inst_type: str, *, inst_family: str = "", inst_id: str = "", proxy: str = ""
) -> list[OKXInstrument]:
    """交易品种列表(按品类筛选;期权/交割可用 inst_family 缩小,如 BTC-USD)。"""
    if inst_type not in INST_TYPES:
        raise OKXApiError("51000", f"Parameter instType error: {inst_type}", "instruments")
    params = {"instType": inst_type}
    if inst_family:
        params["instFamily"] = inst_family
    if inst_id:
        params["instId"] = inst_id
    rows = okx_get("/api/v5/public/instruments", params, symbol=inst_type, proxy=proxy)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append(
            OKXInstrument(
                inst_id=r.get("instId", ""),
                inst_type=r.get("instType", inst_type),
                inst_family=r.get("instFamily", ""),
                base_ccy=r.get("baseCcy", ""),
                quote_ccy=r.get("quoteCcy", ""),
                settle_ccy=r.get("settleCcy", ""),
                ct_val=_to_float(r.get("ctVal")),
                ct_val_ccy=r.get("ctValCcy", ""),
                ct_type=r.get("ctType", ""),
                lever=r.get("lever", ""),
                list_time=_to_ts(r.get("listTime")),
                exp_time=_to_ts(r.get("expTime")),
                option_type=r.get("optType", ""),
                strike=_to_float(r.get("strike")),
                uly=r.get("uly", ""),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Engine vendor 适配:把 OKX 挂进 quote / kline 主备链。
# crypto 品种用 market="CRYPTO" 隔离,不与 CN/HK/US 股票源竞争;
# symbol 直接用 OKX instId(如 BTC-USDT / BTC-USDT-SWAP / BTC-USD-260920-66000-C)。
# ---------------------------------------------------------------------------


class OKXQuoteVendor:
    """quote 引擎适配:输入 OKX instId 列表,输出 Quote(供主备链/持仓页复用)。"""

    name = "okx"
    supports_markets = {"CRYPTO"}

    def fetch(self, symbols: list, config: dict) -> list:
        if not symbols:
            return []
        proxy = str(config.get("proxy") or "")
        out = []
        for sym in symbols:
            inst_id = sym.code
            try:
                t = fetch_ticker(inst_id, proxy=proxy)
            except OKXApiError as e:
                logger.warning(f"OKX quote {inst_id} 失败: {e}")
                continue
            out.append(_ticker_to_quote(t))
        return out


def _ticker_to_quote(t: OKXTicker):
    from marketdata.types import Quote

    return Quote(
        symbol=t.inst_id,
        market="CRYPTO",
        current_price=t.last or 0.0,
        prev_close=t.open24h,          # 24h 开盘视作昨收
        open_price=t.open24h,
        high_price=t.high24h,
        low_price=t.low24h,
        change_amount=(t.last - t.open24h) if (t.last is not None and t.open24h is not None) else None,
        change_pct=t.change_pct_24h,
        volume=t.vol24h,
        turnover=t.vol_ccy24h,
        timestamp=t.ts or datetime.now(),
    )


class OKXKlineVendor:
    """kline 引擎适配:单 instId,OKX candles → list[Bar]。config 可带 bar(默认 day)。"""

    name = "okx"
    supports_markets = {"CRYPTO"}

    def fetch(self, symbols: list, config: dict) -> list[Bar]:
        if not symbols:
            return []
        inst_id = symbols[0].code
        bar = str(config.get("bar") or config.get("timeframe") or "day")
        proxy = str(config.get("proxy") or "")
        try:
            days = int(config.get("days") or 120)
        except (TypeError, ValueError):
            days = 120
        return fetch_candles(inst_id, bar=bar, limit=days, proxy=proxy)
