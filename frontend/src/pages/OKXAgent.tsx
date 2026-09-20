import { useCallback, useEffect, useRef, useState } from 'react'
import { Bot, RefreshCw, Send, X, Wallet, ListOrdered, History as HistoryIcon, AlertTriangle, Sparkles, Check, Ban, Loader2, MessageSquare } from 'lucide-react'
import {
  getOKXAgentStatus,
  toggleOKXAgent,
  placeOKXAgentOrder,
  cancelOKXAgentOrder,
  getOKXAgentHistory,
  getOKXAgentBalance,
  getOKXAgentPositions,
  getOKXAgentPendingOrders,
  triggerOKXAnalysis,
  stopOKXAnalysis,
  getOKXAnalyzeStatus,
  AnalyzeConflictError,
  getOKXStrategies,
  approveOKXStrategy,
  rejectOKXStrategy,
  streamOKXAnalyzeProgress,
  type OKXAgentStatus,
  type OKXAgentHistoryItem,
  type OKXAgentBalanceItem,
  type OKXAgentPositionItem,
  type OKXPendingOrderItem,
  type TAStrategy,
  type TAProgressEvent,
} from '@panwatch/api'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { InstIdSelect } from '../components/InstIdSelect'
import { AlgoTradingCard } from '../components/AlgoTradingCard'

const statusBadge = (s: string) => {
  const ok = ['live', 'filled']
  const bad = ['failed', 'canceled']
  if (ok.includes(s)) return 'bg-emerald-500/10 text-emerald-600'
  if (bad.includes(s)) return 'bg-red-500/10 text-red-600'
  return 'bg-amber-500/10 text-amber-600'
}

const fmtEq = (v: string | undefined) => {
  const n = parseFloat(v || '0')
  return isNaN(n) ? '—' : n.toLocaleString(undefined, { maximumFractionDigits: 4 })
}

// ---- AI 策略聊天流:事件 → 聊天消息模型 ----

interface ChatMsg {
  key: string
  kind: 'role_start' | 'role_text' | 'role_end' | 'system' | 'strategy'
  stage: string
  label: string
  text: string
  done: boolean
}

const STAGE_LABEL: Record<string, string> = {
  market_analyst: '市场分析师',
  social_analyst: '情绪分析师',
  news_analyst: '新闻分析师',
  fundamentals_analyst: '基本面分析师',
  bull_researcher: '多头研究员',
  bear_researcher: '空头研究员',
  research_manager: '研究主管',
  trader: '交易员',
  aggressive_analyst: '激进派风控',
  conservative_analyst: '保守派风控',
  neutral_analyst: '中立派风控',
  final_decision: '投资组合经理',
}

const stageLabel = (e: TAProgressEvent) =>
  e.stage_label || STAGE_LABEL[e.stage] || (e.stage === 'llm_call' ? '思考中' : e.stage || '系统')

/** 把 SSE 事件流(去重后)折叠成聊天消息列表。 */
const buildChat = (events: TAProgressEvent[]): ChatMsg[] => {
  const msgs: ChatMsg[] = []
  const seen = new Set<number>()
  // 每个角色当前文本消息(同角色多轮 LLM 调用追加同一条消息)
  const stageText = new Map<string, ChatMsg>()
  for (const e of events) {
    if (seen.has(e.id)) continue
    seen.add(e.id)
    const label = stageLabel(e)
    if (e.action === 'stage_start') {
      const m: ChatMsg = { key: `s-${e.id}`, kind: 'role_start', stage: e.stage, label, text: '', done: false }
      msgs.push(m)
    } else if (e.action === 'llm_token' || e.action === 'llm_text') {
      // 无角色归属(llm_call)是回调时序噪音,不渲染
      if (e.stage === 'llm_call' || e.stage === 'error') continue
      const text = e.text || ''
      // llm_text 是全量:过滤空/超短;llm_token 是增量:实时 append
      if (e.action === 'llm_text' && text.trim().length < 20) continue
      if (e.action === 'llm_token' && !text) continue
      let m = stageText.get(e.stage)
      if (!m) {
        m = { key: `t-${e.stage}-${e.id}`, kind: 'role_text', stage: e.stage, label, text: '', done: true }
        stageText.set(e.stage, m)
        msgs.push(m)
      }
      if (e.action === 'llm_token') {
        m.text += text   // 增量:逐 token 实时上屏
      } else {
        // 全量替换:若 token 流已覆盖更长文本则保留,否则替换(幂等防重复)
        if (text.length >= m.text.length) m.text = text
      }
    } else if (e.action === 'stage_end') {
      for (let i = msgs.length - 1; i >= 0; i--) {
        const m = msgs[i]
        if (m.stage === e.stage && m.kind === 'role_start' && !m.done) { m.done = true; break }
      }
    } else if (e.action === 'llm_error' || e.action === 'chain_error') {
      msgs.push({ key: `e-${e.id}`, kind: 'system', stage: '', label: '错误', text: e.text, done: true })
    }
  }
  return msgs
}

export default function OKXAgentPage() {
  const { toast } = useToast()
  const [status, setStatus] = useState<OKXAgentStatus | null>(null)
  const [history, setHistory] = useState<OKXAgentHistoryItem[]>([])
  const [balance, setBalance] = useState<OKXAgentBalanceItem[]>([])
  const [positions, setPositions] = useState<OKXAgentPositionItem[]>([])
  const [pending, setPending] = useState<OKXPendingOrderItem[]>([])
  const [loading, setLoading] = useState(false)

  // 下单表单
  const [instId, setInstId] = useState('BTC-USDT')
  const [side, setSide] = useState('buy')
  const [ordType, setOrdType] = useState('market')
  const [sz, setSz] = useState('0.001')
  const [px, setPx] = useState('')
  const [tdMode, setTdMode] = useState('cash')
  const [clOrdId, setClOrdId] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const [cancelId, setCancelId] = useState('')
  const [cancelling, setCancelling] = useState(false)

  // AI 策略(聊天流)
  const [aiInstId, setAiInstId] = useState('BTC-USDT')
  const [analyzing, setAnalyzing] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [chatMsgs, setChatMsgs] = useState<ChatMsg[]>([])
  const [snapStatus, setSnapStatus] = useState('')
  const [finalNote, setFinalNote] = useState('')
  const [strategies, setStrategies] = useState<TAStrategy[]>([])
  const [approving, setApproving] = useState<number | null>(null)
  const sseAbortRef = useRef<AbortController | null>(null)
  const chatEndRef = useRef<HTMLDivElement | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [st, h] = await Promise.all([
        getOKXAgentStatus(),
        getOKXAgentHistory(100).catch(() => []),
      ])
      setStatus(st)
      setHistory(h)
      // 余额/挂单/持仓只在密钥配置好时拉
      if (st.credentials_configured) {
        const [b, p, o] = await Promise.all([
          getOKXAgentBalance().catch(() => []),
          getOKXAgentPositions().catch(() => []),
          getOKXAgentPendingOrders().catch(() => []),
        ])
        setBalance(b)
        setPositions(p)
        setPending(o)
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : '加载失败', 'error')
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => { load() }, [load])

  // AI 策略:挂载拉一次;分析中由 SSE 驱动
  const loadStrategies = useCallback(async () => {
    try {
      const list = await getOKXStrategies('', '', 30)
      setStrategies(list)
    } catch { /* 静默 */ }
  }, [])

  useEffect(() => {
    loadStrategies()
    return () => { sseAbortRef.current?.abort() }
  }, [loadStrategies])

  // 连接指定 trace 的 SSE 实时流(启动/接管/恢复共用)
  const connectStream = useCallback((traceId: string) => {
    const ac = new AbortController()
    sseAbortRef.current?.abort()
    sseAbortRef.current = ac
    streamOKXAnalyzeProgress(
      traceId,
      (snap) => {
        setChatMsgs(buildChat(snap.events ?? []))
        setSnapStatus(String(snap.status ?? ''))
        // 终态:展示最终决策结论(hold 等无策略场景也能看到结果)
        if (snap.status === 'success' && snap.run?.result) {
          const firstLine = snap.run.result.split('\n').find(l => l.trim()) || ''
          setFinalNote(firstLine.replace(/[#*]/g, '').trim())
        } else if (snap.status === 'failed') {
          setFinalNote(`分析失败:${snap.run?.error || '未知错误'}`)
        } else if (snap.status === 'cancelled') {
          setFinalNote('分析已停止:不再生成策略。')
        }
      },
      { signal: ac.signal },
    ).catch((e: unknown) => {
      if ((e as Error)?.name === 'AbortError') return
      toast(e instanceof Error ? e.message : '实时流中断', 'error')
    }).finally(() => {
      setAnalyzing(false)
      // 终态与策略落库有毫秒级间隔,补拉一次
      setTimeout(() => loadStrategies(), 1500)
      loadStrategies()
    })
  }, [toast, loadStrategies])

  // 挂载/切交易对:若该交易对分析仍在后台跑,自动重连 SSE(停止按钮与思考流可见)
  useEffect(() => {
    let alive = true
    getOKXAnalyzeStatus(aiInstId).then((st) => {
      if (!alive || !st.analyzing || !st.trace_id) return
      setAnalyzing(true)
      connectStream(st.trace_id)
    }).catch(() => { /* 静默 */ })
    return () => { alive = false }
  }, [aiInstId, connectStream])

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [chatMsgs, analyzing])

  const handleAnalyze = async () => {
    if (!aiInstId || !aiInstId.includes('-')) { toast('请填写交易对(如 BTC-USDT)', 'error'); return }
    try {
      setChatMsgs([])
      setSnapStatus('')
      setFinalNote('')
      setAnalyzing(true)
      const r = await triggerOKXAnalysis(aiInstId)
      toast('深度分析已启动,实时输出思考过程', 'success')
      connectStream(r.trace_id)
    } catch (e) {
      if (e instanceof AnalyzeConflictError) {
        // 已在分析中:接管进行中的流(停止按钮出现,思考流继续)
        toast(e.message, 'success')
        if (e.traceId) connectStream(e.traceId)
        else setAnalyzing(false)
        return
      }
      setAnalyzing(false)
      toast(e instanceof Error ? e.message : '提交失败', 'error')
    }
  }

  const handleStop = async () => {
    setStopping(true)
    try {
      const r = await stopOKXAnalysis(aiInstId)
      if (r.accepted) {
        toast('已停止分析,不再生成策略', 'success')
      } else {
        toast(r.message || '该交易对当前无进行中的分析', 'error')
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : '停止失败', 'error')
    } finally {
      setStopping(false)
    }
  }

  const handleApprove = async (s: TAStrategy) => {
    setApproving(s.id)
    try {
      const r = await approveOKXStrategy(s.id, { ord_type: s.ord_type, td_mode: s.td_mode })
      toast(`策略已执行 ordId=${r.ord_id || '-'}`, 'success')
      loadStrategies()
      load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '执行失败', 'error')
      loadStrategies()
    } finally {
      setApproving(null)
    }
  }

  const handleReject = async (s: TAStrategy) => {
    try {
      await rejectOKXStrategy(s.id)
      toast('已拒绝该策略', 'success')
      loadStrategies()
    } catch (e) {
      toast(e instanceof Error ? e.message : '操作失败', 'error')
    }
  }

  const handleToggle = async (enabled: boolean) => {
    try {
      await toggleOKXAgent(enabled)
      setStatus(prev => prev ? { ...prev, enabled } : prev)
      toast(enabled ? 'Agent 自动交易已开启' : 'Agent 自动交易已关闭', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '操作失败', 'error')
    }
  }

  const handlePlaceOrder = async () => {
    if (!instId || !sz) { toast('请填写交易对和数量', 'error'); return }
    if (ordType !== 'market' && !px) { toast('限价单必须填价格', 'error'); return }
    setSubmitting(true)
    try {
      const r = await placeOKXAgentOrder({
        inst_id: instId, side, ord_type: ordType, sz, px,
        td_mode: tdMode, cl_ord_id: clOrdId || undefined,
      })
      toast(`下单成功 ordId=${r.ord_id || r.okx?.ordId || '-'}`, 'success')
      setClOrdId('')
      load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '下单失败', 'error')
    } finally {
      setSubmitting(false)
    }
  }

  const handleCancel = async () => {
    if (!cancelId || !instId) { toast('请填写订单号和交易对', 'error'); return }
    setCancelling(true)
    try {
      await cancelOKXAgentOrder({ inst_id: instId, ord_id: cancelId })
      toast('撤单请求已发送', 'success')
      setCancelId('')
      load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '撤单失败', 'error')
    } finally {
      setCancelling(false)
    }
  }

  const enabled = status?.enabled ?? false
  const credsOk = status?.credentials_configured ?? false
  const mainCcy = balance[0]?.details?.slice(0, 6) ?? []

  return (
    <div className="space-y-4">
      {/* 开关 + 状态 */}
      <div className="card p-4 flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-violet-500/10 flex items-center justify-center">
            <Bot className="w-5 h-5 text-violet-600" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-[15px] font-bold text-foreground">OKX Agent 自动交易</span>
              <Badge variant={credsOk ? 'default' : 'destructive'} className="text-[10px]">
                {credsOk ? (status?.simulated ? '模拟盘' : '实盘') : '未配置密钥'}
              </Badge>
            </div>
            <div className="text-[12px] text-muted-foreground">
              策略通过 OKX V5 签名接口执行交易;密钥从环境变量读取
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </Button>
          <Switch checked={enabled} onCheckedChange={handleToggle} />
          <span className={`text-[12px] font-medium ${enabled ? 'text-emerald-600' : 'text-muted-foreground'}`}>
            {enabled ? '已开启' : '已关闭'}
          </span>
        </div>
      </div>

      {!credsOk && (
        <div className="card p-4 flex items-start gap-2 bg-amber-500/5 border-amber-500/30">
          <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
          <div className="text-[12px] text-muted-foreground leading-relaxed">
            密钥未配置。在 <span className="font-mono">.env</span> 设置{' '}
            <span className="font-mono">OKX_AGENT_API_KEY / SECRET_KEY / PASSPHRASE</span>{' '}
            (模拟盘用 <span className="font-mono">OKX_AGENT_DEMO_*</span> +{' '}
            <span className="font-mono">OKX_AGENT_SIMULATED=1</span>) 后重启后端。API Key 只开交易权限。
          </div>
        </div>
      )}

      {/* AI 策略:聊天流 + 人工确认 */}
      <div className="card p-4">
        <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
          <div className="flex items-center gap-2">
            <Sparkles className="w-4 h-4 text-violet-600" />
            <span className="text-[13px] font-semibold text-foreground">AI 策略 · TradingAgents 多 Agent 决策</span>
            {analyzing && (
              <Badge variant="secondary" className="text-[10px] gap-1">
                <Loader2 className="w-3 h-3 animate-spin" /> {snapStatus || 'running'}
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-2">
            <InstIdSelect value={aiInstId} onChange={setAiInstId} disabled={analyzing} className="w-36" />
            {analyzing ? (
              <>
                <Button size="sm" onClick={handleStop} disabled={stopping} variant="destructive">
                  {stopping ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Ban className="w-3.5 h-3.5" />}
                  {stopping ? '停止中…' : '停止分析'}
                </Button>
                <span className="flex items-center gap-1 text-[12px] text-muted-foreground">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" /> 分析中…
                </span>
              </>
            ) : (
              <Button size="sm" onClick={handleAnalyze}>
                开始分析
              </Button>
            )}
          </div>
        </div>

        {/* 聊天流 */}
        <div className="rounded-lg border border-border bg-accent/10 p-3 h-80 overflow-y-auto scrollbar space-y-3">
          {chatMsgs.length === 0 && !analyzing && (
            <div className="h-full flex flex-col items-center justify-center gap-2 text-muted-foreground">
              <MessageSquare className="w-6 h-6 opacity-40" />
              <div className="text-[12px]">选择交易对发起深度分析,9-Agent 思考过程将实时展示;仓位按账户剩余资金自动决策。</div>
            </div>
          )}
          {chatMsgs.map(m => (
            <div key={m.key} className={m.kind === 'system' ? 'text-[11px] text-red-600 px-2' : ''}>
              {m.kind === 'role_start' && (
                <div className="flex items-center gap-2">
                  <span className={`w-1.5 h-1.5 rounded-full ${m.done ? 'bg-emerald-500' : 'bg-violet-500 animate-pulse'}`} />
                  <span className="text-[12px] font-semibold text-foreground">{m.label}</span>
                  <span className="text-[10px] text-muted-foreground">{m.done ? '完成' : '思考中…'}</span>
                </div>
              )}
              {m.kind === 'role_text' && m.text.trim().length > 5 && (
                <div className="ml-3.5 mt-1 pl-2.5 border-l-2 border-violet-500/30 text-[11px] text-muted-foreground leading-relaxed whitespace-pre-wrap break-words">
                  {m.text.length > 1500 ? m.text.slice(0, 1500) + '…' : m.text}
                </div>
              )}
              {m.kind === 'system' && <div>{m.text}</div>}
            </div>
          ))}
          {analyzing && chatMsgs.length === 0 && (
            <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> 已连接数据流,等待第一个角色入场…
            </div>
          )}
          {!analyzing && finalNote && (
            <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 px-3 py-2 text-[12px] text-foreground leading-relaxed">
              {finalNote}
              {strategies.filter(s => s.status === 'pending').length === 0 && (
                <span className="block mt-1 text-[10px] text-muted-foreground">
                  本轮决策未生成可执行策略(持有/观望不建仓),可在历史策略查看往期记录
                </span>
              )}
            </div>
          )}
          <div ref={chatEndRef} />
        </div>

        {/* 待确认策略(分析完成后) */}
        {strategies.filter(s => s.status === 'pending').length > 0 && (
          <div className="mt-3 space-y-2">
            <div className="text-[11px] font-medium text-muted-foreground">决策完成 · 待人工确认</div>
            {strategies.filter(s => s.status === 'pending').map(s => (
              <div key={s.id} className="rounded-lg border border-violet-500/40 p-3 bg-violet-500/5">
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-mono text-[13px] font-bold text-foreground">{s.inst_id}</span>
                    <Badge variant={s.action === 'buy' ? 'destructive' : 'secondary'} className="text-[10px]">
                      {s.action === 'buy' ? '买入' : '卖出'}
                    </Badge>
                    <span className="text-[11px] text-muted-foreground">置信度 {Number(s.confidence ?? 0).toFixed(1)}/10</span>
                    {s.price_at_analysis ? (
                      <span className="text-[11px] text-muted-foreground font-mono">@{Number(s.price_at_analysis).toLocaleString()}</span>
                    ) : null}
                    {s.duration_ms ? (
                      <span className="text-[11px] text-muted-foreground">{Math.round(s.duration_ms / 1000)}s</span>
                    ) : null}
                    {s.sz && (
                      <Badge variant="outline" className="text-[10px] font-mono">
                        数量 {s.sz}(按剩余资金自动决策)
                      </Badge>
                    )}
                    <span className="text-[10px] text-muted-foreground">{s.created_at?.slice(5, 16)}</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <Button size="sm" onClick={() => handleApprove(s)} disabled={approving === s.id || !enabled || !credsOk}>
                      {approving === s.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                      确认执行
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => handleReject(s)}>
                      <Ban className="w-3.5 h-3.5" />
                    </Button>
                  </div>
                </div>
                {s.reason && (
                  <div className="mt-2 text-[11px] text-muted-foreground leading-relaxed line-clamp-3" title={s.reason}>
                    {s.reason}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {/* 历史策略(折叠,详细) */}
        {strategies.filter(s => s.status !== 'pending').length > 0 && (
          <details className="mt-3">
            <summary className="text-[11px] text-muted-foreground cursor-pointer select-none">
              历史策略({strategies.filter(s => s.status !== 'pending').length})
            </summary>
            <div className="mt-2 space-y-1.5">
              {strategies.filter(s => s.status !== 'pending').map(s => (
                <details key={s.id} className="rounded-lg bg-accent/40 px-2.5 py-2">
                  <summary className="flex items-center justify-between gap-2 cursor-pointer list-none">
                    <div className="flex items-center gap-2 min-w-0 flex-wrap">
                      <span className="font-mono text-[11px] text-foreground font-semibold">{s.inst_id}</span>
                      <span className={`text-[11px] font-bold ${s.action === 'buy' ? 'text-red-600' : s.action === 'sell' ? 'text-green-600' : 'text-muted-foreground'}`}>
                        {s.action_label || (s.action === 'buy' ? '买入' : s.action === 'sell' ? '卖出' : '持有')}
                      </span>
                      <span className="text-[10px] text-muted-foreground">置信度 {Number(s.confidence ?? 0).toFixed(1)}</span>
                      {s.price_at_analysis ? (
                        <span className="text-[10px] text-muted-foreground font-mono">@{Number(s.price_at_analysis).toLocaleString()}</span>
                      ) : null}
                      {s.duration_ms ? (
                        <span className="text-[10px] text-muted-foreground">{Math.round(s.duration_ms / 1000)}s</span>
                      ) : null}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {s.error_msg && <span className="text-[10px] text-red-600 max-w-40 truncate" title={s.error_msg}>{s.error_msg}</span>}
                      <Badge variant="outline" className="text-[10px]">{s.status}</Badge>
                      {s.ord_id && <span className="font-mono text-[10px] text-muted-foreground">{s.ord_id}</span>}
                    </div>
                  </summary>
                  <div className="mt-1.5 space-y-1 text-[10px] text-muted-foreground">
                    <div className="flex flex-wrap gap-x-3">
                      <span>时间 {s.created_at?.slice(0, 19)}</span>
                      {s.analysis_date && <span>分析日 {s.analysis_date}</span>}
                      {s.sz && <span className="font-mono">数量 {s.sz}</span>}
                      {s.model_label && <span>模型 {s.model_label}</span>}
                    </div>
                    {s.reason && (
                      <div className="whitespace-pre-wrap leading-relaxed max-h-48 overflow-y-auto rounded bg-background/60 p-2 font-mono">
                        {s.reason}
                      </div>
                    )}
                  </div>
                </details>
              ))}
            </div>
          </details>
        )}
      </div>

      {/* 策略委托(Algo Trading):风控 + 人工确认 */}
      <AlgoTradingCard enabled={enabled} credsOk={credsOk} />

      {/* 余额 / 持仓 */}
      {credsOk && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="card p-4">
            <div className="flex items-center gap-2 mb-3">
              <Wallet className="w-4 h-4 text-muted-foreground" />
              <span className="text-[13px] font-semibold text-foreground">账户余额</span>
            </div>
            {mainCcy.length === 0 ? (
              <div className="text-[12px] text-muted-foreground">暂无数据</div>
            ) : (
              <div className="grid grid-cols-2 gap-2">
                {mainCcy.map(d => (
                  <div key={d.ccy} className="flex items-center justify-between px-2 py-1.5 rounded bg-accent/40">
                    <span className="font-mono text-[12px] text-muted-foreground">{d.ccy}</span>
                    <span className="font-mono text-[12px] text-foreground">{fmtEq(d.eq)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="card p-4">
            <div className="flex items-center gap-2 mb-3">
              <ListOrdered className="w-4 h-4 text-muted-foreground" />
              <span className="text-[13px] font-semibold text-foreground">当前持仓</span>
            </div>
            {positions.length === 0 ? (
              <div className="text-[12px] text-muted-foreground">暂无持仓</div>
            ) : (
              <div className="space-y-1.5 max-h-40 overflow-auto scrollbar">
                {positions.map(p => (
                  <div key={`${p.instId}-${p.posSide}`} className="flex items-center justify-between px-2 py-1.5 rounded bg-accent/40">
                    <span className="font-mono text-[12px] text-foreground">{p.instId}</span>
                    <span className={`text-[11px] ${parseFloat(p.upl) >= 0 ? 'text-red-600' : 'text-green-600'}`}>
                      {p.posSide === 'long' ? '多' : '空'} {p.pos} · 浮盈 {fmtEq(p.upl)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* 下单 / 撤单 */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="card p-4">
          <div className="flex items-center gap-2 mb-3">
            <Send className="w-4 h-4 text-muted-foreground" />
            <span className="text-[13px] font-semibold text-foreground">Agent 下单</span>
            {!enabled && <Badge variant="secondary" className="text-[10px]">开关关闭</Badge>}
          </div>
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2">
              <div>
                <Label className="text-[11px]">交易对</Label>
                <InstIdSelect value={instId} onChange={setInstId} />
              </div>
              <div>
                <Label className="text-[11px]">方向</Label>
                <div className="flex gap-1">
                  {(['buy', 'sell'] as const).map(s => (
                    <button key={s} type="button" onClick={() => setSide(s)}
                      className={`flex-1 text-[12px] py-1.5 rounded transition-colors ${
                        side === s
                          ? s === 'buy' ? 'bg-red-500 text-white' : 'bg-green-500 text-white'
                          : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                      }`}>
                      {s === 'buy' ? '买入' : '卖出'}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div>
                <Label className="text-[11px]">类型</Label>
                <select value={ordType} onChange={e => setOrdType(e.target.value)}
                  className="w-full h-9 text-[13px] rounded-md border border-input bg-background px-2">
                  <option value="market">市价</option>
                  <option value="limit">限价</option>
                  <option value="post_only">Post Only</option>
                  <option value="ioc">IOC</option>
                  <option value="fok">FOK</option>
                </select>
              </div>
              <div>
                <Label className="text-[11px]">数量</Label>
                <Input value={sz} onChange={e => setSz(e.target.value)} placeholder="0.001" className="font-mono text-[13px]" />
              </div>
              <div>
                <Label className="text-[11px]">价格{ordType === 'market' ? '(市价忽略)' : ''}</Label>
                <Input value={px} onChange={e => setPx(e.target.value)} placeholder={ordType === 'market' ? '—' : '50000'} disabled={ordType === 'market'} className="font-mono text-[13px]" />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <Label className="text-[11px]">交易模式</Label>
                <select value={tdMode} onChange={e => setTdMode(e.target.value)}
                  className="w-full h-9 text-[13px] rounded-md border border-input bg-background px-2">
                  <option value="cash">现货(cash)</option>
                  <option value="cross">全仓(cross)</option>
                  <option value="isolated">逐仓(isolated)</option>
                </select>
              </div>
              <div>
                <Label className="text-[11px]">客户端单号(可选)</Label>
                <Input value={clOrdId} onChange={e => setClOrdId(e.target.value)} placeholder="幂等键" className="font-mono text-[13px]" />
              </div>
            </div>
            <Button onClick={handlePlaceOrder} disabled={submitting || !enabled || !credsOk} className="w-full">
              {submitting ? '提交中…' : `发送交易请求 (${side === 'buy' ? '买入' : '卖出'})`}
            </Button>
          </div>
        </div>

        <div className="card p-4">
          <div className="flex items-center gap-2 mb-3">
            <X className="w-4 h-4 text-muted-foreground" />
            <span className="text-[13px] font-semibold text-foreground">撤单</span>
          </div>
          <div className="space-y-3">
            <div>
              <Label className="text-[11px]">交易对</Label>
              <InstIdSelect value={instId} onChange={setInstId} />
            </div>
            <div>
              <Label className="text-[11px]">订单号 (ordId)</Label>
              <Input value={cancelId} onChange={e => setCancelId(e.target.value)} placeholder="如 684239165651906560" className="font-mono text-[13px]" />
            </div>
            <Button variant="outline" onClick={handleCancel} disabled={cancelling || !enabled || !credsOk} className="w-full">
              {cancelling ? '撤单中…' : '撤销订单'}
            </Button>
            {pending.length > 0 && (
              <div className="pt-2 border-t border-border">
                <div className="text-[11px] text-muted-foreground mb-2">当前挂单({pending.length})</div>
                <div className="space-y-1.5 max-h-32 overflow-auto scrollbar">
                  {pending.map(o => (
                    <button key={o.ordId} type="button"
                      onClick={() => { setCancelId(o.ordId); setInstId(o.instId) }}
                      className="w-full flex items-center justify-between px-2 py-1.5 rounded bg-accent/40 hover:bg-accent transition-colors text-left">
                      <span className="font-mono text-[11px] text-foreground">{o.instId} {o.side === 'buy' ? '买' : '卖'} {o.sz}@{o.px}</span>
                      <span className="font-mono text-[10px] text-muted-foreground">{o.ordId}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 执行历史 */}
      <div className="card p-4">
        <div className="flex items-center gap-2 mb-3">
          <HistoryIcon className="w-4 h-4 text-muted-foreground" />
          <span className="text-[13px] font-semibold text-foreground">执行历史</span>
          <span className="text-[11px] text-muted-foreground">({history.length})</span>
        </div>
        {history.length === 0 ? (
          <div className="text-[12px] text-muted-foreground py-4 text-center">暂无记录</div>
        ) : (
          <div className="overflow-x-auto scrollbar">
            <table className="w-full text-[12px]">
              <thead>
                <tr className="text-left text-muted-foreground border-b border-border">
                  <th className="py-2 pr-3 font-medium">时间</th>
                  <th className="py-2 pr-3 font-medium">交易对</th>
                  <th className="py-2 pr-3 font-medium">方向</th>
                  <th className="py-2 pr-3 font-medium">类型</th>
                  <th className="py-2 pr-3 font-medium">数量</th>
                  <th className="py-2 pr-3 font-medium">价格</th>
                  <th className="py-2 pr-3 font-medium">状态</th>
                  <th className="py-2 pr-3 font-medium">错误</th>
                </tr>
              </thead>
              <tbody>
                {history.map(h => (
                  <tr key={h.id} className="border-b border-border/50 hover:bg-accent/30">
                    <td className="py-2 pr-3 text-muted-foreground whitespace-nowrap">{h.created_at?.slice(5, 19)}</td>
                    <td className="py-2 pr-3 font-mono">{h.inst_id}</td>
                    <td className={`py-2 pr-3 font-medium ${h.side === 'buy' ? 'text-red-600' : 'text-green-600'}`}>
                      {h.side === 'buy' ? '买入' : '卖出'}
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">{h.ord_type}</td>
                    <td className="py-2 pr-3 font-mono">{h.sz}</td>
                    <td className="py-2 pr-3 font-mono">{h.px || '—'}</td>
                    <td className="py-2 pr-3">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${statusBadge(h.status)}`}>{h.status}</span>
                    </td>
                    <td className="py-2 pr-3 text-red-600 max-w-40 truncate" title={h.error_msg}>{h.error_code ? `${h.error_code} ${h.error_msg}`.trim() : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
