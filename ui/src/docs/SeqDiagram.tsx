import { Background, BaseEdge, Controls, EdgeLabelRenderer, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, getStraightPath, useReactFlow, type Edge, type EdgeProps, type Node, type NodeProps } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo, useState } from 'react'
import { wrap } from './text'
import { Frame } from './Frame'
import type { Sequence } from './mermaid'

type Mark = 'future' | 'current' | 'past'
const COL = 330, HEAD = 56, ROW_GAP = 26

function Head({ data }: NodeProps<Node<{ label: string }>>) { return <div className="vk-actor">{data.label}</div> }
function Life({ data }: NodeProps<Node<{ h: number }>>) { return <div className="vk-life" style={{ height: data.h }} /> }
function Anchor() {
  return <div className="vk-anchor"><Handle id="l" type="source" position={Position.Left} isConnectable={false} /><Handle id="lt" type="target" position={Position.Left} isConnectable={false} />
    <Handle id="r" type="source" position={Position.Right} isConnectable={false} /><Handle id="rt" type="target" position={Position.Right} isConnectable={false} /></div>
}
function Note({ data }: NodeProps<Node<{ text: string; mark: Mark; w: number }>>) { return <div className={`vk-note ${data.mark}`} style={{ width: data.w }}>{wrap(data.text, Math.floor(data.w / 7.2)).map((l, i) => <div key={i}>{l}</div>)}</div> }
function Block({ data }: NodeProps<Node<{ text: string; w: number }>>) { return <div className="vk-block" style={{ width: data.w }}>{data.text}</div> }

interface MsgData extends Record<string, unknown> { text: string; dashed: boolean; lost: boolean; mark: Mark; index: number }
function Msg({ id, sourceX, sourceY, targetX, targetY, data, markerEnd }: EdgeProps<Edge<MsgData>>) {
  const [path, lx, ly] = getStraightPath({ sourceX, sourceY, targetX, targetY })
  const d = data!
  const color = d.lost ? '#fb7185' : d.mark === 'current' ? '#34f5a2' : '#22d3ee'
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={d.lost ? undefined : markerEnd} style={{ stroke: color, strokeWidth: d.mark === 'current' ? 2.4 : 1.6, strokeDasharray: d.dashed || d.lost ? '6 4' : undefined, opacity: d.mark === 'future' ? 0.18 : 1 }} />
      <EdgeLabelRenderer>
        <div className={`vk-msg ${d.mark} ${d.lost ? 'lost' : ''}`} style={{ transform: `translate(-50%, -100%) translate(${lx}px, ${ly - 4}px)` }}>
          <i>{d.index}</i>{d.lost ? '✕ ' : ''}{wrap(d.text, 38).map((l, i) => <span key={i}>{l}</span>)}
        </div>
      </EdgeLabelRenderer>
    </>
  )
}

const nodeTypes = { head: Head, life: Life, anchor: Anchor, note: Note, block: Block }
const edgeTypes = { msg: Msg }

interface Laid { nodes: Node[]; steps: { y: number; msg?: number; label: string }[]; total: number; width: number; height: number }

function layout(seq: Sequence): Laid {
  const x = new Map(seq.participants.map((p, i) => [p.id, i * COL + 120]))
  const nodes: Node[] = []
  const steps: Laid['steps'] = []
  let y = HEAD + 28
  let n = 0
  let msgNo = 0
  const label = (id: string) => seq.participants.find((p) => p.id === id)?.label ?? id
  let depth = 0
  seq.items.forEach((it, i) => {
    if (it.t === 'block') { const w = (seq.participants.length - 1) * COL + 200; nodes.push({ id: `b${i}`, type: 'block', position: { x: 20, y }, data: { text: `${it.kind.toUpperCase()}  ${it.text}`, w }, draggable: false, selectable: false, zIndex: -1 }); y += 34; depth++; return }
    if (it.t === 'end') { depth--; y += 10; return }
    if (it.t === 'msg') {
      const lines = wrap(it.text, 38).length
      y += 18 + lines * 15
      const a = x.get(it.from)!, b = x.get(it.to)!
      const right = a < b
      msgNo++
      nodes.push({ id: `s${i}`, type: 'anchor', position: { x: a, y }, data: {}, draggable: false, selectable: false, width: 1, height: 1 })
      nodes.push({ id: `t${i}`, type: 'anchor', position: { x: b, y }, data: { right }, draggable: false, selectable: false, width: 1, height: 1 })
      steps.push({ y, msg: i, label: `${label(it.from)} → ${label(it.to)}: ${it.text.replace(/\n/g, ' ')}` })
      y += ROW_GAP
      n++
      return
    }
    const cols = it.over.map((id) => x.get(id)!)
    const w = it.over.length > 1 ? Math.abs(cols[1] - cols[0]) + 150 : 250
    const cx = it.over.length > 1 ? (cols[0] + cols[1]) / 2 : it.side === 'right' ? cols[0] + 150 : it.side === 'left' ? cols[0] - 150 : cols[0]
    const lines = wrap(it.text, Math.floor(w / 7.2)).length
    nodes.push({ id: `n${i}`, type: 'note', position: { x: cx - w / 2, y }, data: { text: it.text, w, mark: 'future' }, draggable: false, selectable: false })
    steps.push({ y, label: `Note: ${it.text.replace(/\n/g, ' ')}` })
    y += lines * 17 + 22 + ROW_GAP
    n++
  })
  const height = y + 10
  seq.participants.forEach((p) => {
    nodes.unshift({ id: `life-${p.id}`, type: 'life', position: { x: x.get(p.id)!, y: HEAD }, data: { h: height - HEAD }, draggable: false, selectable: false, zIndex: -2 })
    nodes.unshift({ id: `head-${p.id}`, type: 'head', position: { x: x.get(p.id)! - 80, y: 0 }, data: { label: p.label }, draggable: false, selectable: false, width: 160, height: 40 })
  })
  void depth
  return { nodes, steps, total: n, width: (seq.participants.length - 1) * COL + 240, height }
}

function Body({ seq, fullscreen }: { seq: Sequence; fullscreen: boolean }) {
  const laid = useMemo(() => layout(seq), [seq])
  const rf = useReactFlow()
  const [step, setStep] = useState(laid.total)  // total = everything shown
  const [playing, setPlaying] = useState(false)

  useEffect(() => {
    if (!playing) return
    const t = setInterval(() => setStep((s) => { if (s >= laid.total) { setPlaying(false); return s } return s + 1 }), 1100)
    return () => clearInterval(t)
  }, [playing, laid.total])
  useEffect(() => { const t = setTimeout(() => void rf.fitView({ padding: 0.08, duration: 300 }), 50); return () => clearTimeout(t) }, [rf, fullscreen, laid])

  // item index -> step number (1-based) for messages and notes
  const stepOf = useMemo(() => { const m = new Map<number, number>(); let k = 0; seq.items.forEach((it, i) => { if (it.t === 'msg' || it.t === 'note') m.set(i, ++k) }); return m }, [seq])
  const mark = (i: number): Mark => { const s = stepOf.get(i)!; return s === step ? 'current' : s < step ? 'past' : 'future' }
  const all = step >= laid.total && !playing

  const nodes = useMemo(() => laid.nodes.map((n) => (n.type === 'note' ? { ...n, data: { ...n.data, mark: all ? 'past' : mark(Number(n.id.slice(1))) } } : n)), [laid, step, all]) // eslint-disable-line react-hooks/exhaustive-deps
  const edges: Edge[] = useMemo(() => {
    const out: Edge[] = []
    let no = 0
    seq.items.forEach((it, i) => {
      if (it.t !== 'msg') { return }
      no++
      const right = seq.participants.findIndex((p) => p.id === it.from) < seq.participants.findIndex((p) => p.id === it.to)
      const m = all ? 'past' : mark(i)
      out.push({
        id: `m${i}`, type: 'msg', source: `s${i}`, target: `t${i}`, sourceHandle: right ? 'r' : 'l', targetHandle: right ? 'lt' : 'rt', animated: m === 'current',
        data: { text: it.text, dashed: it.dashed, lost: it.lost, mark: m, index: no } satisfies MsgData, markerEnd: { type: MarkerType.ArrowClosed, color: m === 'current' ? '#34f5a2' : '#22d3ee' },
      })
    })
    return out
  }, [seq, step, all]) // eslint-disable-line react-hooks/exhaustive-deps

  const height = fullscreen ? undefined : Math.min(640, Math.max(300, laid.height * 0.85 + 30))
  const cur = laid.steps[step - 1]
  return (
    <>
      <div className="vk-canvas" style={{ height }}>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} fitView minZoom={0.15} maxZoom={1.6} nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}
          zoomOnScroll={false} preventScrolling={false} colorMode="dark">
          <Background gap={24} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <div className="vk-bar">
        <button className="small" onClick={() => { if (all) setStep(0); setPlaying(!playing) }}>{playing ? '⏸ Pause' : '▶ Play'}</button>
        <button className="small ghost" disabled={step <= 1 && !all} onClick={() => { setPlaying(false); setStep(Math.max(1, (all ? laid.total : step) - 1)) }}>◀ Prev</button>
        <button className="small ghost" disabled={step >= laid.total} onClick={() => { setPlaying(false); setStep(step + 1) }}>Next ▶</button>
        <button className="small ghost" disabled={all} onClick={() => { setPlaying(false); setStep(laid.total) }}>Show all</button>
        <span className="vk-now">{all ? <small className="muted">{laid.total} steps · press Play to replay them one by one</small> : <><small className="muted">Step {step}/{laid.total}</small> <b>{cur?.label}</b></>}</span>
      </div>
    </>
  )
}

export default function SeqDiagram({ seq, title }: { seq: Sequence; title: string }) {
  return (
    <Frame title={title} kind="Sequence diagram">
      {(fullscreen) => <ReactFlowProvider><Body seq={seq} fullscreen={fullscreen} /></ReactFlowProvider>}
    </Frame>
  )
}
