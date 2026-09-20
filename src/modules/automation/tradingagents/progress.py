"""TradingAgents 进度回调。

走两个机制:
1. LangChain `BaseCallbackHandler`:LLM 每次调用前后的 hook
2. LangGraph 节点切换:通过 debug=True 流式输出捕获(可选)

进度写入 PanWatch 的 `log_context`,前端轮询 `/api/agents/runs/{trace_id}/progress`
聚合返回阶段。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from src.platform.observability.log_context import log_context
from src.platform.observability import otel

logger = logging.getLogger(__name__)

# ---- 活跃 handler 注册表:trace_id -> handler(供停止分析联动取消) ----
_active_handlers: dict[str, "PanWatchProgressHandler"] = {}
_active_handlers_lock = threading.Lock()


def cancel_handler(trace_id: str) -> bool:
    """按 trace_id 取消活跃分析进度。返回是否找到 handler。"""
    with _active_handlers_lock:
        h = _active_handlers.get(trace_id)
        if h is None:
            return False
        h.cancel()
        return True


def active_trace_ids() -> list[str]:
    """当前活跃(可取消)的 trace_id 列表。"""
    with _active_handlers_lock:
        return list(_active_handlers.keys())


# 默认阶段映射:TradingAgents 4 个 analyst + 辩论 + 风控 + PM
STAGES_ORDER = [
    "market_analyst",
    "social_analyst",
    "news_analyst",
    "fundamentals_analyst",
    "bull_bear_debate",
    "research_manager",
    "trader",
    "risk_judge",
    "final_decision",
]

# 阶段 → 中文角色名(前端聊天流展示)
STAGE_DISPLAY = {
    "market_analyst": "市场分析师",
    "social_analyst": "情绪分析师",
    "news_analyst": "新闻分析师",
    "fundamentals_analyst": "基本面分析师",
    "bull_bear_debate": "多空辩论",
    "bull_researcher": "多头研究员",
    "bear_researcher": "空头研究员",
    "research_manager": "研究主管",
    "trader": "交易员",
    "risk_judge": "风控辩论",
    "aggressive_analyst": "激进派",
    "conservative_analyst": "保守派",
    "neutral_analyst": "中立派",
    "final_decision": "投资组合经理",
}


try:
    from langchain_core.callbacks import BaseCallbackHandler as _LCBaseCallbackHandler
    _LANGCHAIN_AVAILABLE = True
except ImportError:  # tradingagents 未装时仍允许 import 本模块,测试不依赖
    _LANGCHAIN_AVAILABLE = False

    class _LCBaseCallbackHandler:  # type: ignore[no-redef]
        """Fallback stub when langchain_core 未安装。"""
        pass


class PanWatchProgressHandler(_LCBaseCallbackHandler):
    """LangChain BaseCallbackHandler 兼容的进度处理器。

    新版 langchain (1.x) 把 callbacks 字段用 pydantic 校验为 BaseCallbackHandler 实例,
    所以必须继承上游基类才能被接受。

    覆盖核心 hook:
    - on_llm_start: 某个 LLM 调用开始(可推断当前在哪个 analyst)
    - on_llm_end: LLM 调用结束,带成本
    - on_chain_start/end: LangGraph 节点切换

    P0 简单实现:把所有事件都 logger.info 出来,带 trace_id 标签。
    前端通过过滤 log_entries 表的 trace_id + event=ta_progress 拿到时间线。
    """

    def __init__(self, trace_id: str, agent_name: str = "tradingagents"):
        # langchain_core BaseCallbackHandler 没有 __init__ 参数,直接 super 安全
        try:
            super().__init__()
        except TypeError:
            # 某些版本要求无参,某些要求带参,兜底
            pass
        self.trace_id = trace_id
        self.agent_name = agent_name
        self._started_at = time.monotonic()
        self._total_cost = 0.0
        self._completed_stages: set[str] = set()
        self._current_stage: str = ""     # on_chain_start 设置,on_chain_end 清空
        self._last_stage: str = ""        # 最近一次 on_chain_start 的 stage(llm 归属兜底)
        self._stream_buffer: list[str] = []  # token 缓冲(streaming 可用时)
        self._cancelled = False              # 协作式取消:置位后所有回调 no-op
        # OTel 桥接:handler 在异步侧构造(to_thread 之前),此处捕获当前上下文,
        # 供工作线程里的 callback 把节点/LLM 子 span 挂到 root span 下(关闭时为 None)。
        self._otel_parent = otel.capture_context()
        self._otel_stage_spans: dict[str, Any] = {}
        self._otel_llm_span: Any = None
        # 注册到全局表,供停止分析按 trace_id 取消
        with _active_handlers_lock:
            _active_handlers[self.trace_id] = self

    @property
    def elapsed_sec(self) -> float:
        return time.monotonic() - self._started_at

    def cancel(self) -> None:
        """请求取消:后续所有回调 no-op(分析线程自然跑完或尽早短路)。"""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _emit(self, stage: str, action: str, **extra):
        """写一条进度日志。前端按 trace_id + event=ta_progress 拉。"""
        if self._cancelled:
            return
        with log_context(
            trace_id=self.trace_id,
            agent_name=self.agent_name,
            event="ta_progress",
            tags={
                "stage": stage,
                "action": action,
                "elapsed_sec": round(self.elapsed_sec, 2),
                "total_cost_usd": round(self._total_cost, 6),
                **extra,
            },
        ):
            logger.info(f"[TA进度] stage={stage} action={action} {extra}")

    def _emit_llm_text(self, action: str, text: str, **extra) -> None:
        """写一条 LLM 输出文本事件(带当前角色名),供前端聊天流渲染。"""
        if self._cancelled:
            return
        stage = self._current_stage or self._last_stage or "llm_call"
        with log_context(
            trace_id=self.trace_id,
            agent_name=self.agent_name,
            event="ta_progress",
            tags={
                "stage": stage,
                "stage_label": STAGE_DISPLAY.get(stage, stage),
                "action": action,
                "elapsed_sec": round(self.elapsed_sec, 2),
                "total_cost_usd": round(self._total_cost, 6),
                "text": (text or "")[:6000],
                **extra,
            },
        ):
            logger.info(f"[TA进度] stage={stage} action={action} chars={len(text or '')}")

    # ---- LangChain callbacks 接口 ----

    # 关键:LLM 默认按 token 估算成本(deepseek-chat 单价),后续可由调用方注入更精确单价
    _PRICE_PER_M_PROMPT = 0.14
    _PRICE_PER_M_COMPLETION = 0.28

    def on_llm_start(self, serialized, prompts, **kwargs):
        self._llm_call_count = getattr(self, "_llm_call_count", 0) + 1
        self._emit("llm_call", "llm_start", call_n=self._llm_call_count)
        self._stream_buffer = []
        # OTel:TA 的一次 LLM 调用 -> gen_ai 子 span(遵循 GenAI 语义约定)。
        model = ""
        try:
            model = (
                (kwargs.get("invocation_params") or {}).get("model")
                or (serialized or {}).get("name")
                or ""
            )
        except Exception:
            model = ""
        self._otel_llm_span = otel.start_detached_span(
            f"chat {model}".strip() if model else "chat",
            parent_context=self._otel_parent,
            attributes={
                otel.GEN_AI_SYSTEM: "tradingagents",
                otel.GEN_AI_OPERATION_NAME: "chat",
                **({otel.GEN_AI_REQUEST_MODEL: model} if model else {}),
            },
        )

    def on_llm_end(self, response, **kwargs):
        # langchain LLMResult.llm_output 含 token_usage
        usage = {}
        try:
            usage = (response.llm_output or {}).get("token_usage") or {}
        except Exception:
            pass
        prompt_tokens = usage.get("prompt_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or 0
        # 累加成本估算
        cost = (
            prompt_tokens / 1_000_000 * self._PRICE_PER_M_PROMPT
            + completion_tokens / 1_000_000 * self._PRICE_PER_M_COMPLETION
        )
        self.record_cost(cost)
        self._emit(
            "llm_call",
            "llm_end",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            call_cost=round(cost, 6),
        )
        # 输出该次 LLM 完整文本(带角色名) → 前端聊天流渲染。
        # 先 flush token 缓冲残余(避免与全量文本重复),再发全量兜底。
        self._flush_stream_buffer(final=True)
        text = self._llm_text(response)
        if text:
            self._emit_llm_text("llm_text", text)
        # OTel:回填 token 用量并结束 gen_ai span。
        if self._otel_llm_span is not None:
            otel.set_span_attributes(
                self._otel_llm_span,
                {
                    otel.GEN_AI_USAGE_INPUT_TOKENS: int(prompt_tokens),
                    otel.GEN_AI_USAGE_OUTPUT_TOKENS: int(completion_tokens),
                },
            )
            otel.end_span(self._otel_llm_span)
            self._otel_llm_span = None

    def on_llm_new_token(self, token: str, **kwargs):
        """token 级流式回调(LLM streaming=True 时触发)。缓冲攒批写日志,避免每 token 一条。"""
        if self._cancelled or not token:
            return
        self._stream_buffer.append(str(token))
        if sum(len(t) for t in self._stream_buffer) >= 80:
            self._flush_stream_buffer()

    def _flush_stream_buffer(self, final: bool = False) -> None:
        if not self._stream_buffer:
            return
        chunk = "".join(self._stream_buffer)
        self._stream_buffer = []
        self._emit_llm_text("llm_token", chunk, flush=final)

    @staticmethod
    def _llm_text(response) -> str:
        """从 LLMResult 提取生成文本(容错多种结构)。"""
        try:
            gens = response.generations
            if gens and gens[0]:
                g = gens[0][0]
                return str(getattr(g, "text", "") or "")
        except Exception:
            pass
        return ""

    def on_chain_start(self, serialized, inputs, **kwargs):
        # LangGraph 节点切换;name 形如 "Market Analyst" / "Bull Researcher" 等
        name = (
            (kwargs.get("name") or "")
            or (serialized or {}).get("name", "")
            or "unknown"
        )
        stage = _normalize_stage(name)
        if not stage:
            return
        # 每次都刷新当前角色:同一节点可能多次 start(如多轮辩论),
        # _current_stage 供 llm_token/llm_text 归属角色,必须实时跟进
        self._current_stage = stage
        self._last_stage = stage
        if stage not in self._completed_stages:
            self._emit(stage, "stage_start", langgraph_node=name,
                       stage_label=STAGE_DISPLAY.get(stage, stage))
            # OTel:TradingAgents 节点 -> 子 span(挂到 root span 下)。
            if stage not in self._otel_stage_spans:
                span = otel.start_detached_span(
                    f"tradingagents.stage {stage}",
                    parent_context=self._otel_parent,
                    attributes={
                        otel.ATTR_TA_STAGE: stage,
                        otel.ATTR_AGENT_NAME: self.agent_name,
                    },
                )
                if span is not None:
                    self._otel_stage_spans[stage] = span

    def on_chain_end(self, outputs, **kwargs):
        name = (kwargs.get("name") or "").strip()
        stage = _normalize_stage(name)
        if stage:
            self._flush_stream_buffer(final=True)  # 收尾:把 token 缓冲残余推完
            self._completed_stages.add(stage)
            self._emit(stage, "stage_end", langgraph_node=name,
                       stage_label=STAGE_DISPLAY.get(stage, stage))
            # 注意:不清空 _current_stage。langchain 回调时序里 llm_end 常晚于
            # 对应节点的 chain_end 到达,清空会让 llm_text 归属到 llm_call(无角色名),
            # 前端聊天流无法按角色渲染。保留最后一次 stage 作为兜底归属。
            # OTel:结束该节点 span。
            span = self._otel_stage_spans.pop(stage, None)
            if span is not None:
                otel.end_span(span)

    def on_llm_error(self, error, **kwargs):
        self._emit("error", "llm_error", error=str(error)[:200])

    def on_chain_error(self, error, **kwargs):
        self._emit("error", "chain_error", error=str(error)[:200])

    # ---- 公共方法 ----

    def record_cost(self, usd: float) -> None:
        self._total_cost += usd

    def _guess_stage(self, serialized: dict, kwargs: dict) -> str:
        name = (serialized.get("name") or kwargs.get("name") or "unknown").lower()
        return _normalize_stage(name) or "unknown"


def _normalize_stage(name: str) -> str:
    """把 LangGraph 节点名标准化到已知阶段值(含辩论/风控子角色)。"""
    n = (name or "").lower().replace(" ", "_")
    # 先精确匹配子角色(避免 "Bull Researcher" 被 "research_manager" 误吃)
    exact = {
        "bull_researcher": "bull_researcher",
        "bear_researcher": "bear_researcher",
        "aggressive_analyst": "aggressive_analyst",
        "conservative_analyst": "conservative_analyst",
        "neutral_analyst": "neutral_analyst",
        "market_analyst": "market_analyst",
        "sentiment_analyst": "social_analyst",   # 上游节点名是 Sentiment Analyst
        "news_analyst": "news_analyst",
        "fundamentals_analyst": "fundamentals_analyst",
        "research_manager": "research_manager",
        "trader": "trader",
        "portfolio_manager": "final_decision",
    }
    if n in exact:
        return exact[n]
    for stage in STAGES_ORDER:
        if stage in n or n in stage:
            return stage
    return ""


def aggregate_progress(log_entries: list[dict]) -> dict:
    """读 log_entries 表里 event=ta_progress 的记录,聚合成阶段进度。

    log_entries 行结构(参考 src/web/log_handler.py):
    {timestamp, level, logger_name, message, trace_id, agent_name, event, tags, ...}
    tags 是 dict,含 stage / action / elapsed_sec / total_cost_usd 等。

    返回结构(给前端):
    {
        "current_stage": "bull_bear_debate",
        "completed_stages": [...],
        "started_at": ...,
        "elapsed_sec": 123.4,
        "total_cost_usd": 0.018,
        "stages": [
            {"name": "market_analyst", "status": "done", "duration_sec": 12.3, "cost_usd": 0.004},
            ...
        ]
    }
    """
    stage_state: dict[str, dict] = {s: {"name": s, "status": "pending"} for s in STAGES_ORDER}
    total_cost = 0.0
    current_stage = None
    started_at = None

    for entry in log_entries:
        tags = entry.get("tags") or {}
        stage = tags.get("stage")
        action = tags.get("action")
        ts = entry.get("timestamp")
        if started_at is None and ts:
            started_at = ts

        if not stage or stage not in stage_state:
            continue

        # cost 累积取最后一条的 total_cost_usd
        cost = tags.get("total_cost_usd")
        if cost is not None:
            total_cost = max(total_cost, float(cost))

        if action == "stage_start":
            stage_state[stage]["status"] = "running"
            stage_state[stage]["started_at"] = ts
            current_stage = stage
        elif action == "stage_end":
            stage_state[stage]["status"] = "done"
            if "started_at" in stage_state[stage] and ts:
                # 简略时长(实际 ts 是 datetime,这里依赖调用方转换)
                pass

    return {
        "current_stage": current_stage,
        "completed_stages": [s for s, v in stage_state.items() if v["status"] == "done"],
        "started_at": started_at,
        "elapsed_sec": float(log_entries[-1].get("tags", {}).get("elapsed_sec", 0))
        if log_entries
        else 0,
        "total_cost_usd": round(total_cost, 6),
        "stages": [stage_state[s] for s in STAGES_ORDER],
    }


# ---- 进度快照构建(automation.api 与 okx_agent.api 共用,避免跨模块 import api 层) ----

def build_progress_snapshot(db, trace_id: str) -> dict:
    """从 log_entries + agent_runs 聚合一次完整进度快照。

    结构同 GET /api/agents/runs/{trace_id}/progress:
    status / current_stage / completed_stages / elapsed_sec / total_cost_usd /
    stages / events(原始事件流,含各角色思考文本)/ toolkit_* / run。
    """
    from datetime import datetime, timezone

    from src.platform.persistence.models import AgentRun, LogEntry

    logs = (
        db.query(LogEntry)
        .filter(
            LogEntry.trace_id == trace_id,
            LogEntry.event.in_(["ta_progress", "ta_toolkit"]),
        )
        .order_by(LogEntry.id.asc())
        .limit(500)
        .all()
    )

    def _fmt_ts(ts):
        return ts.isoformat() if ts is not None else None

    log_dicts = [
        {
            "id": le.id,
            "timestamp": _fmt_ts(le.timestamp),
            "level": le.level,
            "message": le.message,
            "event": le.event,
            "tags": le.tags or {},
            "_ts": le.timestamp,
        }
        for le in logs
    ]

    progress_logs = [d for d in log_dicts if d.get("event") == "ta_progress"]
    progress = aggregate_progress(progress_logs)

    # 原始事件流(带自增 id),供前端聊天流按序渲染各角色思考文本。
    progress["events"] = [
        {
            "id": d["id"],
            "ts": d["timestamp"],
            "stage": (d.get("tags") or {}).get("stage") or "",
            "stage_label": (d.get("tags") or {}).get("stage_label") or "",
            "action": (d.get("tags") or {}).get("action") or "",
            "text": (d.get("tags") or {}).get("text") or "",
            "elapsed_sec": (d.get("tags") or {}).get("elapsed_sec"),
        }
        for d in progress_logs
    ]

    # 工具调用诊断
    toolkit_logs = [d for d in log_dicts if d.get("event") == "ta_toolkit"]
    toolkit_summary = {"hit": 0, "miss": 0, "passthrough": 0, "fallthrough": 0, "error": 0}
    toolkit_recent = []
    for d in toolkit_logs:
        tags = d.get("tags") or {}
        action = (tags.get("action") or "").lower()
        if action in toolkit_summary:
            toolkit_summary[action] += 1
        toolkit_recent.append({
            "timestamp": d.get("timestamp"),
            "action": tags.get("action"),
            "method": tags.get("method"),
            "symbol": tags.get("symbol"),
            "reason": tags.get("reason"),
            "chars": tags.get("chars"),
            "snippet": tags.get("snippet"),
            "source": tags.get("source"),
        })
    progress["toolkit_summary"] = toolkit_summary
    progress["toolkit_recent"] = toolkit_recent[-50:]

    run = (
        db.query(AgentRun)
        .filter(AgentRun.trace_id == trace_id)
        .order_by(AgentRun.id.desc())
        .first()
    )

    if run:
        status = run.status
        progress["run"] = {
            "agent_name": run.agent_name,
            "status": run.status,
            "result": (run.result or "")[:1000],
            "error": (run.error or "")[:500],
            "duration_ms": run.duration_ms,
            "model_label": run.model_label,
            "notify_sent": run.notify_sent,
        }
    elif log_dicts:
        # 僵尸 running 检测:最后一条日志距今 > 5 分钟视为中断
        last_log = logs[-1]
        last_ts = last_log.timestamp
        if last_ts is not None:
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            idle_sec = (datetime.now(timezone.utc) - last_ts).total_seconds()
            status = "stale" if idle_sec > 300 else "running"
        else:
            status = "running"
    else:
        status = "not_found"

    progress["trace_id"] = trace_id
    # 取消检测:活跃 handler 已被取消 → 终态 cancelled(SSE 尽快关流)
    with _active_handlers_lock:
        h = _active_handlers.get(trace_id)
    if h is not None and getattr(h, "cancelled", False) and status == "running":
        status = "cancelled"
    progress["status"] = status
    return progress
