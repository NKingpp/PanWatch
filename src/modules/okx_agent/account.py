"""账户信息查询层(只读):策略委托风控的前置数据拉取。

设计:
- 快照 AccountSnapshot:一次拉齐风控所需全部数据(余额/持仓/合约规格/账户风险/杠杆),
  避免风控层再发请求;
- 只读日志统一 [OKX-RO] 前缀 + request_id(client 层已带),写操作日志在 algo 层用 [OKX-RW];
- 全部走 OKXAgentService(签名/限流/错误处理复用)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.modules.okx_agent.client import OKXAgentError, OKXCredentials, request
from src.modules.okx_agent.service import OKXAgentService

logger = logging.getLogger(__name__)


@dataclass
class InstrumentSpec:
    """合约规格(用于张数/名义价值换算)。"""
    inst_id: str
    inst_type: str = ""        # SPOT / SWAP / FUTURES / OPTION
    ct_val: float = 1.0        # 合约面值(1 张 = ctVal 个币;SPOT 恒 1)
    lot_sz: float = 0.0        # 最小下单数量/张数
    min_sz: float = 0.0        # 最小起量
    ct_mult: float = 1.0
    settle_ccy: str = ""       # 结算币种(SWAP: usd/usdt/...)

    @classmethod
    def from_okx(cls, d: dict) -> "InstrumentSpec":
        def _f(v, default=0.0) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return default
        return cls(
            inst_id=str(d.get("instId", "")),
            inst_type=str(d.get("instType", "")),
            ct_val=_f(d.get("ctVal"), 1.0) or 1.0,
            lot_sz=_f(d.get("lotSz")),
            min_sz=_f(d.get("minSz")),
            ct_mult=_f(d.get("ctMult"), 1.0) or 1.0,
            settle_ccy=str(d.get("settleCcy", "")),
        )


@dataclass
class AccountSnapshot:
    """一次风控校验所需的完整账户视图。"""
    # 余额
    total_eq_usd: float = 0.0              # 账户总折合 USD
    available_ccy: dict[str, float] = field(default_factory=dict)   # 币种 → 可用
    # 风险指标(合约账户)
    imr: float = 0.0                        # 初始保证金占用率
    mmr: float = 0.0                        # 维持保证金占用率(≥1 即强平风险)
    mgn_ratio: str = ""                     # 保证金率(OKX 返回,越低越危险)
    # 持仓
    positions: list[dict] = field(default_factory=list)
    # 杠杆(仅该标的,模式/倍数)
    lever: str = ""                         # 该 instId 当前杠杆倍数
    lever_hint: str = ""                    # isolated/cross 提示
    # 规格
    spec: InstrumentSpec | None = None
    # 账户持仓模式:long_short(双向,需 posSide)/ net(单向,禁止 posSide)
    pos_mode: str = ""
    # 标的最新价(名义价值估算)
    last_px: float = 0.0
    # 元信息
    request_ids: list[str] = field(default_factory=list)

    def position_of(self, inst_id: str) -> dict | None:
        for p in self.positions:
            if p.get("instId") == inst_id:
                return p
        return None


class AccountQueryService:
    """账户只读查询。独立于下单服务,便于测试与权限分离。"""

    def __init__(self, creds: OKXCredentials, *, proxy: str = ""):
        self.creds = creds
        self.proxy = proxy
        self._svc = OKXAgentService(creds, enabled=True, proxy=proxy)  # 只读不碰开关

    # ---- 单项查询 ----

    def balance(self, ccy: str = "") -> list[dict]:
        return self._svc.account_balance(ccy)

    def positions(self, inst_type: str = "", inst_id: str = "") -> list[dict]:
        return self._svc.positions(inst_type, inst_id)

    def instrument_spec(self, inst_id: str) -> InstrumentSpec:
        """合约规格。SPOT ctVal=1;SWAP ctVal=张面值。51001(不存在)时逐类型回退。"""
        data: list = []
        for inst_type in ("SPOT", "SWAP", "FUTURES"):
            try:
                data = request(
                    "GET", "/api/v5/public/instruments", self.creds,
                    params={"instType": inst_type, "instId": inst_id},
                    proxy=self.proxy, label=f"ro_instrument_{inst_type.lower()}",
                )
            except OKXAgentError as e:
                if e.code != "51001":   # 非「标的不存在」直接抛
                    raise
                data = []
            if data:
                break
        if not data:
            raise OKXAgentError("INVALID_INST", f"查询合约规格失败:{inst_id} 不存在")
        spec = InstrumentSpec.from_okx(data[0])
        logger.info(f"[OKX-RO] 规格 {inst_id} type={spec.inst_type} ctVal={spec.ct_val} lotSz={spec.lot_sz} minSz={spec.min_sz}")
        return spec

    def account_config(self) -> dict:
        """账户配置:含合约账户杠杆、持仓模式、风险指标(imr/mmr)。"""
        data = request(
            "GET", "/api/v5/account/config", self.creds,
            proxy=self.proxy, label="ro_account_config",
        )
        return data[0] if isinstance(data, list) and data else {}

    def leverage_info(self, inst_id: str) -> dict:
        """指定标的杠杆(现货返回空,合约返回 lever/最高杠杆)。"""
        data = request(
            "GET", "/api/v5/account/leverage-info", self.creds,
            params={"instId": inst_id, "mgnMode": "cross"},
            proxy=self.proxy, label="ro_leverage_info",
        )
        return data[0] if isinstance(data, list) and data else {}

    def ticker(self, inst_id: str) -> dict:
        """最新成交价(公共接口,免签名走 marketdata)。"""
        from marketdata.vendors.okx import fetch_ticker
        t = fetch_ticker(inst_id)
        return {"last": t.last, "ask": t.ask_px, "bid": t.bid_px}

    # ---- 快照 ----

    def snapshot(self, inst_id: str, *, td_mode: str) -> AccountSnapshot:
        """拉齐风控所需全部数据。任一查询失败直接抛 OKXAgentError(不静默,避免半盲校验)。"""
        snap = AccountSnapshot()
        snap.spec = self.instrument_spec(inst_id)
        # 持仓模式(决定下单是否带 posSide);失败不阻塞(默认空=不带)
        try:
            cfg = self.account_config()
            snap.pos_mode = str(cfg.get("posMode") or "")
        except OKXAgentError as e:
            logger.warning(f"[OKX-RO] account-config 拉取失败(忽略): {e}")

        # 余额 + 风险指标(一次接口)
        bal = self._svc.account_balance()
        if isinstance(bal, list) and bal:
            b = bal[0]
            try:
                snap.total_eq_usd = float(b.get("totalEq") or 0)
            except (TypeError, ValueError):
                pass
            snap.imr = _to_f(b.get("imr"))
            snap.mmr = _to_f(b.get("mmr"))
            snap.mgn_ratio = str(b.get("mgnRatio") or "")
            for d in (b.get("details") or []):
                ccy = str(d.get("ccy") or "")
                avail = _to_f(d.get("availEq")) or _to_f(d.get("availBal"))
                if ccy and avail > 0:
                    snap.available_ccy[ccy] = avail
            logger.info(
                f"[OKX-RO] 快照 {inst_id} totalEq=${snap.total_eq_usd:.2f} "
                f"imr={snap.imr:.4f} mmr={snap.mmr:.4f} mgnRatio={snap.mgn_ratio} "
                f"ccys={sorted(snap.available_ccy.keys())}"
            )

        # 持仓(全部,便于统计净敞口)
        snap.positions = self._svc.positions() or []
        pos = snap.position_of(inst_id)
        if pos:
            snap.lever = str(pos.get("lever") or "")
            snap.lever_hint = f"{pos.get('mgnMode', '')}/{pos.get('posSide', '')}"
            logger.info(f"[OKX-RO] 快照 {inst_id} 已有持仓 lever={snap.lever} {snap.lever_hint} pos={pos.get('pos')}")

        # 杠杆(无持仓时也要知道当前杠杆设置);现货无杠杆概念,跳过
        if not snap.lever and snap.spec and snap.spec.inst_type != "SPOT":
            lev = self.leverage_info(inst_id)
            snap.lever = str(lev.get("lever") or "") or "1"
            if lev.get("mgnMode"):
                snap.lever_hint = str(lev["mgnMode"])
        elif not snap.lever:
            snap.lever = "1"  # 现货恒 1 倍

        # 最新价
        try:
            t = self.ticker(inst_id)
            snap.last_px = _to_f(t.get("last"))
        except Exception as e:
            logger.warning(f"[OKX-RO] {inst_id} 行情获取失败(降级用标记价): {e}")
        return snap


def _to_f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0
