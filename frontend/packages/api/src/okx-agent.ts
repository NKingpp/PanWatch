import { fetchAPI } from './client'

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
  fetchAPI<{ queued: boolean; inst_id: string; message: string }>('/okx-agent/analyze', {
    method: 'POST',
    body: JSON.stringify({ inst_id: instId }),
  })

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
  body: { sz: string; ord_type?: string; px?: string; td_mode?: string },
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
