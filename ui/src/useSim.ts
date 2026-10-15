import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type LogEntry, type Meta, type Run, type Snapshot } from './api'

const MAX_LOG = 3000

export interface SimState {
  connected: boolean
  meta: Meta | null
  stations: Record<string, Snapshot>
  order: string[]
  logs: Record<string, LogEntry[]>
  runs: Record<string, Run>
}

/** One WebSocket to the simulator: station snapshots, log lines and scenario progress arrive as events. */
export function useSim(): SimState & { refresh: () => Promise<void>; clearLog: (id: string) => void } {
  const [connected, setConnected] = useState(false)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [stations, setStations] = useState<Record<string, Snapshot>>({})
  const [order, setOrder] = useState<string[]>([])
  const [logs, setLogs] = useState<Record<string, LogEntry[]>>({})
  const [runs, setRuns] = useState<Record<string, Run>>({})
  const pending = useRef<Record<string, LogEntry[]>>({})
  const lastId = useRef<Record<string, number>>({})
  const removedIds = useRef<Set<string>>(new Set())

  // log lines arrive in bursts: batch them into one render every 150 ms
  useEffect(() => {
    const id = setInterval(() => {
      const batch = pending.current
      if (Object.keys(batch).length === 0) return
      pending.current = {}
      setLogs((prev) => {
        const next = { ...prev }
        for (const [sid, rows] of Object.entries(batch)) next[sid] = [...(next[sid] ?? []), ...rows].slice(-MAX_LOG)
        return next
      })
    }, 150)
    return () => clearInterval(id)
  }, [])

  const loadHistory = useCallback(async (sid: string) => {
    const r = await api<{ entries: LogEntry[]; last_id: number }>(`/api/stations/${sid}/log?limit=1000`)
    lastId.current[sid] = r.last_id
    setLogs((prev) => ({ ...prev, [sid]: r.entries }))
  }, [])

  const refresh = useCallback(async () => {
    const list = await api<Snapshot[]>('/api/stations')
    setStations(Object.fromEntries(list.map((s) => [s.id, s])))
    setOrder(list.map((s) => s.id))
  }, [])

  useEffect(() => { void api<Meta>('/api/meta').then(setMeta).catch(() => undefined) }, [])

  useEffect(() => {
    let socket: WebSocket | null = null
    let closed = false
    let retry: ReturnType<typeof setTimeout>
    const open = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${proto}://${location.host}/ws`)
      socket.onopen = () => setConnected(true)
      socket.onclose = (e) => {
        setConnected(false)
        if (e.code === 4401) { window.dispatchEvent(new Event('sim-unauthorized')); return }  // not logged in: stop retrying
        if (!closed) retry = setTimeout(open, 1500)
      }
      socket.onmessage = (m) => {
        const ev = JSON.parse(m.data)
        if (ev.type === 'init') {
          const list = ev.stations as Snapshot[]
          setStations(Object.fromEntries(list.map((s) => [s.id, s])))
          setOrder(list.map((s) => s.id))
          list.forEach((s) => void loadHistory(s.id))
        } else if (ev.type === 'state') {
          if (removedIds.current.has(ev.station_id)) return
          const snap = ev.state as Snapshot
          setStations((prev) => ({ ...prev, [ev.station_id]: snap }))
          setOrder((prev) => (prev.includes(ev.station_id) ? prev : [...prev, ev.station_id]))
          if (!(ev.station_id in lastId.current)) { lastId.current[ev.station_id] = 0; void loadHistory(ev.station_id) }
        } else if (ev.type === 'log') {
          if (ev.entry.id > (lastId.current[ev.station_id] ?? 0)) {
            lastId.current[ev.station_id] = ev.entry.id
            ;(pending.current[ev.station_id] ??= []).push(ev.entry)
          }
        } else if (ev.type === 'scenario') {
          setRuns((prev) => ({ ...prev, [ev.station_id]: ev.run }))
        } else if (ev.type === 'removed') {
          removedIds.current.add(ev.station_id)
          setStations((prev) => { const n = { ...prev }; delete n[ev.station_id]; return n })
          setOrder((prev) => prev.filter((x) => x !== ev.station_id))
        }
      }
    }
    open()
    const ping = setInterval(() => socket?.readyState === 1 && socket.send('ping'), 20000)
    return () => { closed = true; clearTimeout(retry); clearInterval(ping); socket?.close() }
  }, [loadHistory])

  const clearLog = useCallback((id: string) => {
    void api(`/api/stations/${id}/log`, { method: 'DELETE' })
    setLogs((prev) => ({ ...prev, [id]: [] }))
  }, [])

  return { connected, meta, stations, order, logs, runs, refresh, clearLog }
}
