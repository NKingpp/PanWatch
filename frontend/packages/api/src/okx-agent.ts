import { fetchAPI, getToken } from './client'

export interface OKXAgentStatus {
  enabled: boolean
  credentials_configured: boolean
  simulated: boolean
}

export interface OKXAgentOrderResult {
  ord_id: string
  cl_ord_id: string
  status: string
  okx: Record<string, unknown>
}

export interface OKXAgentHistoryItem {
  id: number
  inst_id: string
  side: string
  ord_type: string
  td_mode: string
  sz: string
  px: string
  cl_ord_id: string
  ord_id: string | null
  status: string
  error_code: string
  error_msg: string
  created_at: string
  updated_at: string
}

export interface OKXAgentBalanceItem {
  details?: Array<{ ccy: string; eq: string; eqUsd: string }>
  totalEq?: string
}

export interface OKXAgentPositionItem {
  instId: string
  posSide: string
  pos: string
  avgPx: string
  upl: string
  uplRatio: string
  lever: string
  mgnMode: string
}

export interface OKXPendingOrderItem {
  ordId: string
  instId: string
  side: string
  px: string
  sz: string
  state: string
}

export const getOKXAgentStatus = () => fetchAPI<OKXAgentStatus>('/okx-agent/status')

export const toggleOKXAgent = (enabled: boolean) =>
  fetchAPI<{ enabled: boolean }>('/okx-agent/toggle', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  })

export const placeOKXAgentOrder = (body: {
  inst_id: string
  side: string
  ord_type?: string
  sz: string
  px?: string
  td_mode?: string
  cl_ord_id?: string
  reduce_only?: boolean | string
}) =>
  fetchAPI<OKXAgentOrderResult>('/okx-agent/orders', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const cancelOKXAgentOrder = (body: { inst_id: string; ord_id?: string; cl_ord_id?: string }) =>
  fetchAPI<Record<string, unknown>>('/okx-agent/orders/cancel', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const getOKXAgentHistory = (limit = 50) =>
  fetchAPI<OKXAgentHistoryItem[]>(`/okx-agent/history?limit=${limit}`)

export const getOKXAgentBalance = (ccy = '') =>
  fetchAPI<OKXAgentBalanceItem[]>(`/okx-agent/balance${ccy ? `?ccy=${ccy}` : ''}`)

export const getOKXAgentPositions = (instType = '', instId = '') => {
  const p = new URLSearchParams()
  if (instType) p.set('inst_type', instType)
  if (instId) p.set('inst_id', instId)
  const qs = p.toString()
  return fetchAPI<OKXAgentPositionItem[]>(`/okx-agent/positions${qs ? `?${qs}` : ''}`)
}

export const getOKXAgentPendingOrders = (instType = '', instId = '') => {
  const p = new URLSearchParams()
  if (instType) p.set('inst_type', instType)
  if (instId) p.set('inst_id', instId)
  const qs = p.toString()
  return fetchAPI<OKXPendingOrderItem[]>(`/okx-agent/orders/pending${qs ? `?${qs}` : ''}`)
}

// ---------- TradingAgents AI 策略 ----------

export interface TAStrategy {
  id: number
  inst_id: string
  action: 'buy' | 'sell' | 'hold'
  action_label: string
  rating_raw: string
  confidence: number
  ord_type: string
  td_mode: string
  sz: string
  px: string
  reason: string
  trace_id: string
  analysis_date: string
  status: 'pending' | 'approved' | 'executed' | 'rejected' | 'failed' | 'expired'
  ord_id: string | null
  error_msg: string
  created_at: string
  updated_at: string
}

export const triggerOKXAnalysis = (instId: string) =>
  fetchAPI<{ queued: boolean; inst_id: string; trace_id: string; message: string }>(
    '/okx-agent/analyze',
    { method: 'POST', body: JSON.stringify({ inst_id: instId }) },
  )

// ---------- AI 策略实时流(SSE) ----------

export interface TAProgressEvent {
  id: number
  ts: string
  stage: string
  stage_label: string
  action: string
  text: string
  elapsed_sec: number | null
}

export interface TAProgressSnapshot {
  trace_id: string
  status: 'running' | 'success' | 'failed' | 'stale' | 'not_found' | string
  current_stage: string | null
  completed_stages: string[]
  elapsed_sec: number
  total_cost_usd: number
  stages: Array<{ name: string; status: string }>
  events: TAProgressEvent[]
  run?: { status: string; result: string; error: string; duration_ms: number }
}

/**
 * 订阅 AI 策略分析实时流(各角色思考过程)。
 * 用 fetch 手写 SSE 解析(EventSource 不支持 Authorization header)。
 * onProgress 每次收到快照调用;流结束(终态/超时/错误)后 resolve。
 */
export async function streamOKXAnalyzeProgress(
  traceId: string,
  onProgress: (snap: TAProgressSnapshot) => void,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  const token = getToken()
  const res = await fetch(
    `/api/okx-agent/analyze/stream?trace_id=${encodeURIComponent(traceId)}`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      signal: opts?.signal,
    },
  )
  if (!res.ok || !res.body) throw new Error(`SSE 连接失败 (HTTP ${res.status})`)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  const parseEvent = (raw: string) => {
    let event = 'message'
    const dataLines: string[] = []
    for (const line of raw.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
    }
    if (!dataLines.length) return null
    try {
      return { event, data: JSON.parse(dataLines.join('\n')) }
    } catch {
      return null
    }
  }

  // eslint-disable-next-line no-constant-condition
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const ev = parseEvent(raw)
      if (!ev) continue
      if (ev.event === 'progress' && ev.data) {
        onProgress(ev.data as TAProgressSnapshot)
      } else if (ev.event === 'done') {
        return
      }
    }
  }
}

export const getOKXAnalyzeStatus = (instId: string) =>
  fetchAPI<{ inst_id: string; analyzing: boolean }>(
    `/okx-agent/analyze/status?inst_id=${encodeURIComponent(instId)}`,
  )

export const getOKXStrategies = (status = '', instId = '', limit = 50) => {
  const p = new URLSearchParams()
  if (status) p.set('status', status)
  if (instId) p.set('inst_id', instId)
  p.set('limit', String(limit))
  return fetchAPI<TAStrategy[]>(`/okx-agent/strategies?${p.toString()}`)
}

export const approveOKXStrategy = (
  strategyId: number,
  body: { sz?: string; ord_type?: string; px?: string; td_mode?: string },
) =>
  fetchAPI<{ strategy_id: number; ord_id: string; status: string }>(
    `/okx-agent/strategies/${strategyId}/approve`,
    { method: 'POST', body: JSON.stringify(body) },
  )

export const rejectOKXStrategy = (strategyId: number) =>
  fetchAPI<{ strategy_id: number; status: string }>(
    `/okx-agent/strategies/${strategyId}/reject`,
    { method: 'POST', body: JSON.stringify({}) },
  )

// ==================== 策略委托(Algo Trading) ====================

export interface OKXAlgoRiskConfig {
  enabled: boolean
  max_notional_usd: number
  max_notional_pct: number
  max_margin_pct: number
  max_total_margin_pct: number
  imr_hard_limit: number
  fee_buffer_pct: number
  min_net_usd: number
}

export interface OKXAlgoRiskCheck {
  ok: boolean
  notional_usd: number
  est_margin_usd: number
  available_usd: number
  net_usd: number
  lever: number
  violations: Array<{ code: string; msg: string }>
}

export interface OKXAlgoSnapshot {
  total_eq_usd: number
  available_ccy: Record<string, number>
  imr: number
  mmr: number
  mgn_ratio: string
  lever: string
  lever_hint: string
  positions_count: number
  inst_position: { pos: string; avgPx: string; lever: string; mgnMode: string } | null
  last_px: number
}

export interface OKXAlgoProposal {
  id: number
  inst_id: string
  td_mode: string
  side: string
  ord_type: string
  sz: string
  order_px: string
  tp_trigger_px: string
  sl_trigger_px: string
  trigger_px: string
  reduce_only: number
  status: string
  algo_id: string | null
  s_code: string
  s_msg: string
  risk_check: string | null
  snapshot_before: string | null
  snapshot_after: string | null
  created_at: string
  updated_at: string
}

export interface OKXAlgoProposalBody {
  inst_id: string
  td_mode: string
  side: string
  ord_type: string
  sz: string
  order_px?: string
  trigger_px?: string
  tp_trigger_px?: string
  tp_ord_px?: string
  sl_trigger_px?: string
  sl_ord_px?: string
  callback_ratio?: string
  move_trigger_px?: string
  reduce_only?: boolean
}

export interface OKXAlgoProposalOutcome {
  proposal: OKXAlgoProposal
  risk: OKXAlgoRiskCheck | null
  ok: boolean
  message: string
}

export const createOKXAlgoProposal = (body: OKXAlgoProposalBody) =>
  fetchAPI<OKXAlgoProposalOutcome>('/okx-agent/algo/proposal', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const confirmOKXAlgoProposal = (pid: number) =>
  fetchAPI<OKXAlgoProposalOutcome>(`/okx-agent/algo/proposals/${pid}/confirm`, {
    method: 'POST',
    body: JSON.stringify({}),
  })

export const rejectOKXAlgoProposal = (pid: number) =>
  fetchAPI<OKXAlgoProposal>(`/okx-agent/algo/proposals/${pid}/reject`, {
    method: 'POST',
    body: JSON.stringify({}),
  })

export const getOKXAlgoProposals = (status = '', instId = '', limit = 50) => {
  const p = new URLSearchParams()
  if (status) p.set('status', status)
  if (instId) p.set('inst_id', instId)
  p.set('limit', String(limit))
  return fetchAPI<OKXAlgoProposal[]>(`/okx-agent/algo/proposals?${p.toString()}`)
}

export const getOKXAlgoPending = (instId = '') =>
  fetchAPI<Array<Record<string, string>>>(
    `/okx-agent/algo/orders/pending${instId ? `?inst_id=${encodeURIComponent(instId)}` : ''}`,
  )

export const cancelOKXAlgoOrders = (items: Array<{ algo_id: string; inst_id: string }>) =>
  fetchAPI<Array<Record<string, string>>>('/okx-agent/algo/orders/cancel', {
    method: 'POST',
    body: JSON.stringify({ items }),
  })

export const getOKXAlgoRiskConfig = () => fetchAPI<OKXAlgoRiskConfig>('/okx-agent/algo/risk-config')

export const updateOKXAlgoRiskConfig = (body: Partial<OKXAlgoRiskConfig>) =>
  fetchAPI<OKXAlgoRiskConfig>('/okx-agent/algo/risk-config', {
    method: 'PUT',
    body: JSON.stringify(body),
  })

