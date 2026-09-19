import { useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Loader2 } from 'lucide-react'

interface SearchItem { symbol: string; name: string; market: string }

interface Props {
  value: string
  onChange: (v: string) => void
  disabled?: boolean
  placeholder?: string
  className?: string
}

/** OKX 交易对选择器:输入联想 + 下拉选择(数据源 /api/stocks/search?market=CRYPTO) */
export function InstIdSelect({ value, onChange, disabled, placeholder = 'BTC-USDT', className = '' }: Props) {
  const [items, setItems] = useState<SearchItem[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const timer = useRef<number | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  const doSearch = async (q: string) => {
    setLoading(true)
    try {
      const list = await fetchAPI<SearchItem[]>(`/stocks/search?q=${encodeURIComponent(q)}&market=CRYPTO&limit=20`)
      setItems(list)
      setOpen(true)
    } catch { setItems([]) }
    finally { setLoading(false) }
  }

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  const handleInput = (v: string) => {
    onChange(v.toUpperCase())
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => doSearch(v.trim().toUpperCase()), 400)
  }

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <Input
        value={value}
        onChange={e => handleInput(e.target.value)}
        onFocus={() => { if (items.length === 0) doSearch(value.trim()) ; else setOpen(true) }}
        placeholder={placeholder}
        disabled={disabled}
        className="font-mono text-[13px]"
        autoComplete="off"
      />
      {loading && (
        <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none" />
      )}
      {open && items.length > 0 && (
        <div className="absolute z-50 mt-1 w-full min-w-48 max-h-60 overflow-auto scrollbar rounded-md border border-border bg-background shadow-lg">
          {items.map(it => (
            <button
              key={it.symbol}
              type="button"
              onClick={() => { onChange(it.symbol); setOpen(false) }}
              className={`w-full text-left px-2.5 py-1.5 hover:bg-accent transition-colors flex items-center justify-between gap-2 ${it.symbol === value ? 'bg-accent/60' : ''}`}
            >
              <span className="font-mono text-[12px] text-foreground">{it.symbol}</span>
              <span className="text-[10px] text-muted-foreground truncate max-w-32">{it.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
