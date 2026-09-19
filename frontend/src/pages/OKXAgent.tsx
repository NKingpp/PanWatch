import { useCallback, useEffect, useRef, useState } from 'react'
import { Bot, RefreshCw, Send, X, Wallet, ListOrdered, History as HistoryIcon, AlertTriangle, Sparkles, Check, Ban, Loader2 } from 'lucide-react'
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
  getOKXAnalyzeStatus,
  getOKXStrategies,
  approveOKXStrategy,
  rejectOKXStrategy,
  type OKXAgentStatus,
  type OKXAgentHistoryItem,
  type OKXAgentBalanceItem,
  type OKXAgentPositionItem,
  type OKXPendingOrderItem,
  type TAStrategy,
} from '@panwatch/api'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { InstIdSelect } from '../components/InstIdSelect'

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

  // AI 策略
  const [aiInstId, setAiInstId] = useState('BTC-USDT')
  const [analyzing, setAnalyzing] = useState(false)
  const [strategies, setStrategies] = useState<TAStrategy[]>([])
  const [approveSz, setApproveSz] = useState<Record<number, string>>({})
  const [approving, setApproving] = useState<number | null>(null)
  const pollRef = useRef<number | null>(null)

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

  // AI 策略:挂载拉一次 + 分析中 10s 轮询
  const loadStrategies = useCallback(async () => {
    try {
      const list = await getOKXStrategies('', '', 30)
      setStrategies(list)
    } catch { /* 静默 */ }
  }, [])

  useEffect(() => {
    loadStrategies()
    getOKXAnalyzeStatus('BTC-USDT').then(s => setAnalyzing(s.analyzing)).catch(() => {})
    return () => { if (pollRef.current) window.clearInterval(pollRef.current) }
  }, [loadStrategies])

  useEffect(() => {
    if (!analyzing) {
      if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null }
      return
    }
    pollRef.current = window.setInterval(async () => {
      try {
        const [s] = await Promise.all([
          getOKXAnalyzeStatus(aiInstId),
          loadStrategies(),
        ])
        if (!s.analyzing) {
          setAnalyzing(false)
          toast('深度分析完成,请查看策略', 'success')
        }
      } catch { /* 忽略轮询错误 */ }
    }, 10_000)
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); pollRef.current = null }
  }, [analyzing, aiInstId, loadStrategies, toast])

  const handleAnalyze = async () => {
    if (!aiInstId || !aiInstId.includes('-')) { toast('请填写交易对(如 BTC-USDT)', 'error'); return }
    try {
      await triggerOKXAnalysis(aiInstId)
      setAnalyzing(true)
      toast('深度分析已提交,预计 3-5 分钟', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '提交失败', 'error')
    }
  }

  const handleApprove = async (s: TAStrategy) => {
    const sz = (approveSz[s.id] || '').trim()
    if (!sz || parseFloat(sz) <= 0) { toast('请填写数量', 'error'); return }
    setApproving(s.id)
    try {
      const r = await approveOKXStrategy(s.id, { sz, ord_type: s.ord_type, td_mode: s.td_mode })
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

      {/* AI 策略(TradingAgents 多 Agent 决策 → 人工确认执行) */}
      <div className="card p-4">
        <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
          <div className="flex items-center gap-2">
            <Sparkles className="w-4 h-4 text-violet-600" />
            <span className="text-[13px] font-semibold text-foreground">AI 策略 · TradingAgents 多 Agent 决策</span>
          </div>
          <div className="flex items-center gap-2">
            <InstIdSelect value={aiInstId} onChange={setAiInstId} disabled={analyzing} className="w-36" />
            <Button size="sm" onClick={handleAnalyze} disabled={analyzing}>
              {analyzing ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> 分析中…</> : '深度分析'}
            </Button>
          </div>
        </div>
        <div className="text-[11px] text-muted-foreground mb-3">
          选择交易对 → 9-Agent 框架深度分析(分析师 → 辩论 → 风控 → PM,3-5 分钟)→ 生成买卖策略 → 你确认后执行。hold/待复核 不生成策略。
        </div>
        {strategies.length === 0 ? (
          <div className="text-[12px] text-muted-foreground py-4 text-center">暂无策略。选择交易对发起深度分析。</div>
        ) : (
          <div className="space-y-2">
            {strategies.map(s => {
              const pending = s.status === 'pending'
              return (
                <div key={s.id} className="rounded-lg border border-border p-3 bg-accent/20">
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-[13px] font-bold text-foreground">{s.inst_id}</span>
                      <Badge variant={s.action === 'buy' ? 'destructive' : 'secondary'} className="text-[10px]">
                        {s.action === 'buy' ? '买入' : s.action === 'sell' ? '卖出' : s.action_label}
                      </Badge>
                      <span className="text-[11px] text-muted-foreground">置信度 {Number(s.confidence ?? 0).toFixed(1)}/10</span>
                      <Badge variant="outline" className="text-[10px]">{s.status}</Badge>
                      <span className="text-[10px] text-muted-foreground">{s.created_at?.slice(5, 16)}</span>
                    </div>
                    {pending && (
                      <div className="flex items-center gap-1.5">
                        <Input
                          value={approveSz[s.id] ?? ''}
                          onChange={e => setApproveSz(prev => ({ ...prev, [s.id]: e.target.value }))}
                          placeholder="数量"
                          className="w-24 h-8 font-mono text-[12px]"
                        />
                        <Button size="sm" onClick={() => handleApprove(s)} disabled={approving === s.id || !enabled || !credsOk}>
                          {approving === s.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                          执行
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => handleReject(s)}>
                          <Ban className="w-3.5 h-3.5" />
                        </Button>
                      </div>
                    )}
                  </div>
                  {s.reason && (
                    <div className="mt-2 text-[11px] text-muted-foreground leading-relaxed line-clamp-3" title={s.reason}>
                      {s.reason}
                    </div>
                  )}
                  {s.error_msg && (
                    <div className="mt-1.5 text-[11px] text-red-600">{s.error_msg}</div>
                  )}
                  {s.ord_id && (
                    <div className="mt-1.5 font-mono text-[10px] text-muted-foreground">ordId: {s.ord_id}</div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

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
