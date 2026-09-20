"""风险校验层:策略委托下单前的本地风控拦截。

原则:
- 只做本地计算(余额/保证金/名义价值 vs 阈值),不再发 OKX 请求;
- 任一违规 → 返回 violations 列表,调用方禁止发起下单;
- 阈值存 app_settings(JSON),运行时可调;enable=False 表示禁用交易(所有提案拒绝)。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.modules.okx_agent.account import AccountSnapshot
from src.modules.okx_agent.client import OKXAgentError

logger = logging.getLogger(__name__)

_SETTING_KEY = "okx_algo_risk_config"


@dataclass
class RiskConfig:
    """风控阈值(可配置)。金额单位 USD。"""
    enabled: bool = True              # False = 模块整体禁用(提案无法创建/确认)
    max_notional_usd: float = 5000.0  # 单笔最大名义价值
    max_notional_pct: float = 30.0    # 单笔名义价值 / 账户总净值 上限(%)
    max_margin_pct: float = 50.0      # 本笔预估保证金 / 账户总净值 上限(%)
    max_total_margin_pct: float = 80.0  # (已有占用 + 本笔) / 总净值 上限(%)
    imr_hard_limit: float = 0.8       # 下单前账户 imr 硬上限(0.8=80% 已占用)
    fee_buffer_pct: float = 0.5       # 现货可用余额额外预留(%)
    min_net_usd: float = 10.0         # 校验时账户总净值下限(净值过低拒绝)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "max_notional_usd": self.max_notional_usd,
            "max_notional_pct": self.max_notional_pct, "max_margin_pct": self.max_margin_pct,
            "max_total_margin_pct": self.max_total_margin_pct, "imr_hard_limit": self.imr_hard_limit,
            "fee_buffer_pct": self.fee_buffer_pct, "min_net_usd": self.min_net_usd,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskConfig":
        cfg = cls()
        if isinstance(d, dict):
            for k in cfg.to_dict():
                if k in d:
                    cur = getattr(cfg, k)
                    try:
                        setattr(cfg, k, type(cur)(d[k]) if cur is not None else d[k])
                    except (TypeError, ValueError):
                        pass
        return cfg


def load_risk_config(db: Session) -> RiskConfig:
    row = db.execute(
        text("SELECT value FROM app_settings WHERE key = :k"), {"k": _SETTING_KEY}
    ).first()
    if not row:
        return RiskConfig()
    try:
        return RiskConfig.from_dict(json.loads(row[0]))
    except (ValueError, TypeError):
        return RiskConfig()


def save_risk_config(db: Session, cfg: RiskConfig) -> None:
    db.execute(
        text("""
INSERT INTO app_settings (key, value, description)
VALUES (:k, :v, 'OKX 策略委托风控阈值')
ON CONFLICT(key) DO UPDATE SET value = :v
"""),
        {"k": _SETTING_KEY, "v": json.dumps(cfg.to_dict(), ensure_ascii=False)},
    )
    db.commit()
    logger.info(f"[风控] 配置更新 {cfg.to_dict()}")


@dataclass
class TradeIntent:
    """待校验的交易意图。sz 语义:SPOT=币数量;SWAP/FUTURES=张数。"""
    inst_id: str
    side: str            # buy / sell
    td_mode: str         # cash / cross / isolated
    sz: str              # 数量或张数
    px: str = ""         # 限价;空=按 last_px 估算
    reduce_only: bool = False


@dataclass
class RiskCheckResult:
    ok: bool
    notional_usd: float = 0.0
    est_margin_usd: float = 0.0
    available_usd: float = 0.0
    net_usd: float = 0.0
    lever: float = 1.0
    violations: list[dict] = field(default_factory=list)  # [{code,msg,detail}]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "notional_usd": round(self.notional_usd, 2),
            "est_margin_usd": round(self.est_margin_usd, 2),
            "available_usd": round(self.available_usd, 2), "net_usd": round(self.net_usd, 2),
            "lever": self.lever, "violations": self.violations,
        }


def _d(v: str | float) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


class RiskChecker:
    """本地风控校验。输入快照 + 意图,输出通过/违规明细。"""

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def validate(self, snap: AccountSnapshot, intent: TradeIntent) -> RiskCheckResult:
        r = RiskCheckResult(ok=True, net_usd=snap.total_eq_usd)
        cfg = self.cfg
        spec = snap.spec

        # ---- 模块级禁用:直接拒绝(不进入下单) ----
        if not cfg.enabled:
            r.ok = False
            r.violations.append({"code": "RISK_DISABLED", "msg": "策略委托模块已禁用"})
            return r

        # ---- 基础数据 ----
        sz = _d(intent.sz)
        if sz <= 0:
            r.ok = False
            r.violations.append({"code": "INVALID_SZ", "msg": f"数量非法: {intent.sz}"})
            return r

        px = _d(intent.px) if intent.px else Decimal(str(snap.last_px or 0))
        if px <= 0:
            r.ok = False
            r.violations.append({"code": "NO_PRICE", "msg": "无法确定价格(行情不可用且未给限价)"})
            return r

        # 张数 → 币数量:SPOT sz=币;SWAP sz=张,1张=ctVal 币
        is_spot = not spec or spec.inst_type == "SPOT"
        coin_sz = sz if is_spot else sz * _d(spec.ct_val if spec else 1)
        notional = coin_sz * px  # USD 计价(USDT/USD 本位近似等值)
        r.notional_usd = float(notional)

        # 最小下单量/步长(合约张数必须整数倍;现货币数量为小数,只查 minSz)
        if spec and spec.min_sz > 0 and sz < _d(spec.min_sz):
            r.ok = False
            r.violations.append({"code": "SZ_TOO_SMALL", "msg": f"数量 {sz} 低于最小 {spec.min_sz}"})
        if not is_spot and spec and spec.lot_sz > 0:
            step = _d(spec.lot_sz)
            if step > 0 and (sz % step) != 0:
                r.ok = False
                r.violations.append({"code": "SZ_NOT_LOT", "msg": f"张数 {sz} 不是步长 {spec.lot_sz} 的整数倍"})

        # ---- 账户净值过低:拒绝一切 ----
        if snap.total_eq_usd < cfg.min_net_usd:
            r.ok = False
            r.violations.append({
                "code": "NET_TOO_LOW", "msg": f"账户净值 ${snap.total_eq_usd:.2f} 低于下限 ${cfg.min_net_usd}",
            })

        # ---- 单笔名义价值上限 ----
        if notional > _d(cfg.max_notional_usd):
            r.ok = False
            r.violations.append({
                "code": "NOTIONAL_CAP",
                "msg": f"名义价值 ${float(notional):,.2f} 超单笔上限 ${cfg.max_notional_usd:,.0f}",
            })

        # ---- 单笔名义 / 净值 ----
        if snap.total_eq_usd > 0:
            pct = float(notional) / snap.total_eq_usd * 100
            if pct > cfg.max_notional_pct:
                r.ok = False
                r.violations.append({
                    "code": "NOTIONAL_PCT",
                    "msg": f"名义价值占净值 {pct:.1f}% 超上限 {cfg.max_notional_pct}%",
                })

        # ---- 按模式分:现货查可用余额;合约查保证金 ----
        if intent.td_mode == "cash":
            # 现货买入:可用计价币(USDT/USDC/USD) ≥ 名义价值 ×(1+缓冲)
            quote = _quote_ccy(intent.inst_id)  # BTC-USDT → USDT
            avail = snap.available_ccy.get(quote, 0.0)
            need = float(notional) * (1 + cfg.fee_buffer_pct / 100)
            r.available_usd = avail
            if intent.side == "buy" and avail < need:
                # reduceOnly 平仓单放行(卖出不需要新买入资金)
                if not intent.reduce_only:
                    r.ok = False
                    r.violations.append({
                        "code": "INSUFFICIENT_BALANCE",
                        "msg": f"可用 {quote} {avail:.4f} 不足(需 {need:.2f},含 {cfg.fee_buffer_pct}% 手续费缓冲)",
                    })
        else:
            # 合约:预估初始保证金 = 名义 / 杠杆
            lever = _d(snap.lever or "1") or Decimal(1)
            r.lever = float(lever)
            margin = notional / lever if lever > 0 else notional
            r.est_margin_usd = float(margin)

            # 本笔保证金 / 净值
            if snap.total_eq_usd > 0:
                mpct = float(margin) / snap.total_eq_usd * 100
                if mpct > cfg.max_margin_pct:
                    r.ok = False
                    r.violations.append({
                        "code": "MARGIN_PCT",
                        "msg": f"本笔保证金占净值 {mpct:.1f}% 超上限 {cfg.max_margin_pct}%",
                    })
                # 已占用 + 本笔(用 imr 近似:imr×净值=已占用保证金)
                used = snap.imr * snap.total_eq_usd
                total_pct = (used + float(margin)) / snap.total_eq_usd * 100
                if total_pct > cfg.max_total_margin_pct:
                    r.ok = False
                    r.violations.append({
                        "code": "TOTAL_MARGIN_PCT",
                        "msg": f"总保证金占用率将达 {total_pct:.1f}% 超上限 {cfg.max_total_margin_pct}%(当前 imr={snap.imr:.2f})",
                    })
            # 可用保证金(折合 USD)兜底
            avail_usd = snap.available_ccy.get("USDT", 0.0) + snap.available_ccy.get("USDC", 0.0)
            r.available_usd = avail_usd
            if float(margin) > avail_usd and not intent.reduce_only:
                r.ok = False
                r.violations.append({
                    "code": "INSUFFICIENT_MARGIN",
                    "msg": f"预估保证金 ${float(margin):,.2f} 超可用(USDT+USDC 折合 ${avail_usd:,.2f})",
                })

        # ---- 账户风险硬顶:imr 已过高(接近强平)拒绝新开仓 ----
        if intent.td_mode != "cash" and not intent.reduce_only and snap.imr >= cfg.imr_hard_limit:
            r.ok = False
            r.violations.append({
                "code": "IMR_TOO_HIGH",
                "msg": f"账户初始保证金占用率 {snap.imr:.2f} 已达硬上限 {cfg.imr_hard_limit}(接近强平,拒绝新开仓)",
            })

        level = "PASS" if r.ok else "BLOCK"
        logger.info(
            f"[风控] {level} {intent.inst_id} {intent.side} {intent.sz}@{px} "
            f"notional=${r.notional_usd:.2f} margin=${r.est_margin_usd:.2f} "
            f"violations={[v['code'] for v in r.violations]}"
        )
        return r


def _quote_ccy(inst_id: str) -> str:
    """BTC-USDT → USDT;BTC-USDT-SWAP → USDT(合约保证金常用 USDT)。"""
    parts = inst_id.split("-")
    return parts[1] if len(parts) >= 2 else "USDT"
