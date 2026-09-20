import { useCallback, useEffect, useState } from 'react'
import { ShieldCheck, ShieldAlert, Check, Ban, Loader2, Settings2, X } from 'lucide-react'
import {
  createOKXAlgoProposal,
  confirmOKXAlgoProposal,
  rejectOKXAlgoProposal,
  getOKXAlgoProposals,
  cancelOKXAlgoOrders,
  getOKXAlgoRiskConfig,
  updateOKXAlgoRiskConfig,
  type OKXAlgoProposal,
  type OKXAlgoRiskConfig,
  type OKXAlgoRiskCheck,
  type OKXAlgoSnapshot,
} from '@panwatch/api'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { InstIdSelect } from '../components/InstIdSelect'

const ORD_TYPES = [
  { v: 'conditional', label: '止盈止损' },
  { v: 'oco', label: 'OCO' },
  { v: 'trigger', label: '计划委托' },
  { v: 'move', label: '移动止盈' },
] as const

const fmtUsd = (v: number | undefined) =>
  v === undefined || v === null ? '—' : `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`

function parseJson<T>(s: string | null | undefined): T | null {
  if (!s) return null
  try { return JSON.parse(s) as T } catch { return null }
}

export function AlgoTradingCard({ enabled, credsOk }: { enabled: boolean; credsOk: boolean }) {
  const { toast } = useToast()
  // 表单
  const [instId, setInstId] = useState('BTC-USDT')
  const [side, setSide] = useState('buy')
  const [tdMode, setTdMode] = useState('cash')
  const [ordType, setOrdType] = useState<'conditional' | 'oco' | 'trigger' | 'move'>('conditional')
  const [sz, setSz] = useState('0.001')
  const [orderPx, setOrderPx] = useState('')
  const [triggerPx, setTriggerPx] = useState('')
  const [tpTriggerPx, setTpTriggerPx] = useState('')
  const [slTriggerPx, setSlTriggerPx] = useState('')
  const [callbackRatio, setCallbackRatio] = useState('')
  const [moveTriggerPx, setMoveTriggerPx] = useState('')
  const [reduceOnly, setReduceOnly] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  // 提案列表 + 确认状态
  const [proposals, setProposals] = useState<OKXAlgoProposal[]>([])
  const [confirming, setConfirming] = useState<number | null>(null)

  // 风控配置
  const [riskCfg, setRiskCfg] = useState<OKXAlgoRiskConfig | null>(null)
  const [showCfg, setShowCfg] = useState(false)
  const [savingCfg, setSavingCfg] = useState(false)

  const loadProposals = useCallback(async () => {
    try { setProposals(await getOKXAlgoProposals('', '', 30)) } catch { /* 静默 */ }
  }, [])

  useEffect(() => {
    loadProposals()
    getOKXAlgoRiskConfig().then(setRiskCfg).catch(() => {})
  }, [loadProposals])

  const handleSubmit = async () => {
    if (!sz) { toast('请填写数量', 'error'); return }
    setSubmitting(true)
    try {
      const out = await createOKXAlgoProposal({
        inst_id: instId, td_mode: tdMode, side, ord_type: ordType, sz,
        order_px: orderPx || undefined,
        trigger_px: ordType === 'trigger' ? triggerPx : undefined,
        tp_trigger_px: (ordType === 'conditional' || ordType === 'oco') ? tpTriggerPx : undefined,
        sl_trigger_px: (ordType === 'conditional' || ordType === 'oco') ? slTriggerPx : undefined,
        callback_ratio: ordType === 'move' ? callbackRatio : undefined,
        move_trigger_px: ordType === 'move' ? moveTriggerPx : undefined,
        reduce_only: reduceOnly,
      })
      if (out.ok) {
        toast('提案已创建,请核对账户快照后确认', 'success')
      } else {
        const codes = out.risk?.violations.map(v => v.msg).join('; ')
        toast(`风控拦截: ${codes || out.message}`, 'error')
      }
      loadProposals()
    } catch (e) {
      toast(e instanceof Error ? e.message : '创建失败', 'error')
    } finally { setSubmitting(false) }
  }

  const handleConfirm = async (p: OKXAlgoProposal) => {
    if (!window.confirm(`确认执行策略委托?\n${p.inst_id} ${p.side === 'buy' ? '买入' : '卖出'} ${p.sz} ${p.ord_type}\n确认后将重新校验风控并提交 OKX`)) return
    setConfirming(p.id)
    try {
      const out = await confirmOKXAlgoProposal(p.id)
      if (out.ok) {
        toast(`已提交 OKX algoId=${out.proposal.algo_id || '-'}`, 'success')
      } else {
        const codes = out.risk?.violations.map(v => v.msg).join('; ')
        toast(`确认时风控拦截: ${codes || out.message}`, 'error')
      }
      loadProposals()
    } catch (e) {
      toast(e instanceof Error ? e.message : '确认失败', 'error')
      loadProposals()
    } finally { setConfirming(null) }
  }

  const handleReject = async (p: OKXAlgoProposal) => {
    try {
      await rejectOKXAlgoProposal(p.id)
      toast('已拒绝', 'success')
      loadProposals()
    } catch (e) {
      toast(e instanceof Error ? e.message : '操作失败', 'error')
    }
  }

  const handleCancelAlgo = async (p: OKXAlgoProposal) => {
    if (!p.algo_id) return
    try {
      await cancelOKXAlgoOrders([{ algo_id: p.algo_id, inst_id: p.inst_id }])
      toast('撤单请求已发送', 'success')
      loadProposals()
    } catch (e) {
      toast(e instanceof Error ? e.message : '撤单失败', 'error')
    }
  }

  const handleSaveCfg = async () => {
    if (!riskCfg) return
    setSavingCfg(true)
    try {
      const saved = await updateOKXAlgoRiskConfig(riskCfg)
      setRiskCfg(saved)
      toast('风控配置已保存', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存失败', 'error')
    } finally { setSavingCfg(false) }
  }

  const needTpSl = ordType === 'conditional' || ordType === 'oco'

  return (
    <div className="card p-4 space-y-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          <ShieldCheck className="w-4 h-4 text-violet-600" />
          <span className="text-[13px] font-semibold text-foreground">策略委托 · 风控 + 人工确认</span>
          <Badge variant={riskCfg?.enabled ? 'default' : 'destructive'} className="text-[10px]">
            {riskCfg ? (riskCfg.enabled ? '风控开启' : '风控禁用') : '…'}
          </Badge>
        </div>
        <Button size="sm" variant="outline" onClick={() => setShowCfg(v => !v)}>
          <Settings2 className="w-3.5 h-3.5" /> 风控配置
        </Button>
      </div>

      {/* 风控配置面板 */}
      {showCfg && riskCfg && (
        <div className="rounded-lg border border-border p-3 bg-accent/20 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-[12px] font-medium">启用策略委托模块</span>
            <div className="flex items-center gap-2">
              <Switch checked={riskCfg.enabled} onCheckedChange={v => setRiskCfg({ ...riskCfg, enabled: v })} />
              <Button size="sm" onClick={handleSaveCfg} disabled={savingCfg}>
                {savingCfg ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null} 保存
              </Button>
            </div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            {([
              ['max_notional_usd', '单笔上限 $'],
              ['max_notional_pct', '名义/净值 %'],
              ['max_margin_pct', '保证金/净值 %'],
              ['max_total_margin_pct', '总保证金/净值 %'],
              ['imr_hard_limit', 'IMR 硬顶'],
              ['fee_buffer_pct', '手续费缓冲 %'],
              ['min_net_usd', '净值下限 $'],
            ] as const).map(([k, label]) => (
              <div key={k}>
                <Label className="text-[10px]">{label}</Label>
                <Input
                  value={String(riskCfg[k])}
                  onChange={e => setRiskCfg({ ...riskCfg, [k]: parseFloat(e.target.value) || 0 })}
                  className="h-8 font-mono text-[12px]"
                />
              </div>
            ))}
          </div>
          <div className="text-[10px] text-muted-foreground">关闭「启用」可立即禁用整个策略委托(新建/确认全部拒绝)。</div>
        </div>
      )}

      {/* 下单表单 */}
      <div className="space-y-3">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
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
          <div>
            <Label className="text-[11px]">模式</Label>
            <select value={tdMode} onChange={e => setTdMode(e.target.value)}
              className="w-full h-9 text-[13px] rounded-md border border-input bg-background px-2">
              <option value="cash">现货(cash)</option>
              <option value="cross">全仓(cross)</option>
              <option value="isolated">逐仓(isolated)</option>
            </select>
          </div>
          <div>
            <Label className="text-[11px]">策略类型</Label>
            <select value={ordType} onChange={e => setOrdType(e.target.value as typeof ordType)}
              className="w-full h-9 text-[13px] rounded-md border border-input bg-background px-2">
              {ORD_TYPES.map(t => <option key={t.v} value={t.v}>{t.label}</option>)}
            </select>
          </div>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <div>
            <Label className="text-[11px]">数量{tdMode !== 'cash' ? '(张)' : ''}</Label>
            <Input value={sz} onChange={e => setSz(e.target.value)} placeholder="0.001" className="font-mono text-[13px]" />
          </div>
          {ordType === 'trigger' && (
            <div>
              <Label className="text-[11px]">触发价</Label>
              <Input value={triggerPx} onChange={e => setTriggerPx(e.target.value)} placeholder="45000" className="font-mono text-[13px]" />
            </div>
          )}
          {needTpSl && (
            <>
              <div>
                <Label className="text-[11px]">止盈触发价</Label>
                <Input value={tpTriggerPx} onChange={e => setTpTriggerPx(e.target.value)} placeholder="55000(可选)" className="font-mono text-[13px]" />
              </div>
              <div>
                <Label className="text-[11px]">止损触发价</Label>
                <Input value={slTriggerPx} onChange={e => setSlTriggerPx(e.target.value)} placeholder="48000(可选)" className="font-mono text-[13px]" />
              </div>
            </>
          )}
          {ordType === 'move' && (
            <>
              <div>
                <Label className="text-[11px]">回调幅度(如 0.05)</Label>
                <Input value={callbackRatio} onChange={e => setCallbackRatio(e.target.value)} placeholder="0.05" className="font-mono text-[13px]" />
              </div>
              <div>
                <Label className="text-[11px]">移动触发价</Label>
                <Input value={moveTriggerPx} onChange={e => setMoveTriggerPx(e.target.value)} placeholder="52000" className="font-mono text-[13px]" />
              </div>
            </>
          )}
          <div>
            <Label className="text-[11px]">委托价(空=市价)</Label>
            <Input value={orderPx} onChange={e => setOrderPx(e.target.value)} placeholder="-1 = 市价" className="font-mono text-[13px]" />
          </div>
        </div>

        <div className="flex items-center justify-between gap-2 flex-wrap">
          <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground cursor-pointer">
            <input type="checkbox" checked={reduceOnly} onChange={e => setReduceOnly(e.target.checked)} className="accent-violet-600" />
            只减仓(reduceOnly)
          </label>
          <Button size="sm" onClick={handleSubmit} disabled={submitting || !enabled || !credsOk}>
            {submitting ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> 校验中…</> : '创建提案(风控校验)'}
          </Button>
        </div>
      </div>

      {/* 提案列表 */}
      <div className="space-y-2">
        <div className="text-[11px] text-muted-foreground">
          流程:创建提案(拉账户+风控) → 人工核对确认(二次风控) → 提交 OKX 算法委托。确认前不下单。
        </div>
        {proposals.length === 0 ? (
          <div className="text-[12px] text-muted-foreground py-3 text-center">暂无提案</div>
        ) : proposals.map(p => {
          const risk = parseJson<OKXAlgoRiskCheck>(p.risk_check)
          const snap = parseJson<OKXAlgoSnapshot>(p.snapshot_before)
          const pendable = p.status === 'pending_confirm'
          const failed = p.status === 'risk_failed' || p.status === 'failed'
          return (
            <div key={p.id} className={`rounded-lg border p-3 ${pendable ? 'border-violet-500/40 bg-violet-500/5' : 'border-border bg-accent/20'}`}>
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-mono text-[13px] font-bold">#{p.id} {p.inst_id}</span>
                  <Badge variant={p.side === 'buy' ? 'destructive' : 'secondary'} className="text-[10px]">
                    {p.side === 'buy' ? '买入' : '卖出'}
                  </Badge>
                  <span className="text-[11px] text-muted-foreground">
                    {ORD_TYPES.find(t => t.v === p.ord_type)?.label || p.ord_type} · {p.sz}{p.reduce_only ? ' · 只减仓' : ''}
                  </span>
                  <Badge variant="outline" className="text-[10px]">{p.status}</Badge>
                  {p.algo_id && <span className="font-mono text-[10px] text-muted-foreground">algo:{p.algo_id}</span>}
                  <span className="text-[10px] text-muted-foreground">{p.created_at?.slice(5, 16)}</span>
                </div>
                <div className="flex items-center gap-1.5">
                  {pendable && (
                    <>
                      <Button size="sm" onClick={() => handleConfirm(p)} disabled={confirming === p.id || !enabled || !credsOk}>
                        {confirming === p.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                        确认执行
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => handleReject(p)}>
                        <Ban className="w-3.5 h-3.5" />
                      </Button>
                    </>
                  )}
                  {p.status === 'confirmed' && p.algo_id && (
                    <Button size="sm" variant="outline" onClick={() => handleCancelAlgo(p)}>
                      <X className="w-3.5 h-3.5" /> 撤单
                    </Button>
                  )}
                </div>
              </div>

              {/* 风控结果 */}
              {risk && (
                <div className="mt-2 text-[11px] space-y-1">
                  <div className="flex items-center gap-3 flex-wrap text-muted-foreground">
                    {risk.ok
                      ? <span className="inline-flex items-center gap-1 text-emerald-600"><ShieldCheck className="w-3 h-3" /> 风控通过</span>
                      : <span className="inline-flex items-center gap-1 text-red-600"><ShieldAlert className="w-3 h-3" /> 风控拦截</span>}
                    <span>名义 {fmtUsd(risk.notional_usd)}</span>
                    {risk.est_margin_usd > 0 && <span>保证金 {fmtUsd(risk.est_margin_usd)}</span>}
                    {risk.lever > 1 && <span>杠杆 {risk.lever}x</span>}
                    <span>净值 {fmtUsd(risk.net_usd)}</span>
                    <span>可用 {fmtUsd(risk.available_usd)}</span>
                  </div>
                  {risk.violations.length > 0 && (
                    <ul className="text-red-600 space-y-0.5">
                      {risk.violations.map((v, i) => <li key={i}>· {v.msg}</li>)}
                    </ul>
                  )}
                </div>
              )}

              {/* 账户快照 */}
              {snap && pendable && (
                <div className="mt-1.5 text-[10px] text-muted-foreground">
                  快照: 总资产 {fmtUsd(snap.total_eq_usd)} · 参考价 {snap.last_px || '—'} · 持仓 {snap.positions_count} 个
                  {snap.inst_position && ` · 本标的 ${snap.inst_position.pos}@${snap.inst_position.avgPx}`}
                  {snap.lever && ` · 杠杆 ${snap.lever}x`}
                </div>
              )}

              {p.s_msg && failed && <div className="mt-1.5 text-[11px] text-red-600">{p.s_code} {p.s_msg}</div>}
            </div>
          )
        })}
      </div>
    </div>
  )
}
