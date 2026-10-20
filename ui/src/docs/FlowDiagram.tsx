import dagre from '@dagrejs/dagre'
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, useReactFlow, type Edge, type Node, type NodeProps } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo, useState } from 'react'
import type { Flow } from './mermaid'
import { Frame } from './Frame'
import { wrap } from './text'

type Mark = 'future' | 'current' | 'past' | 'on' | 'dim' | 'idle'
interface StepData extends Record<string, unknown> { label: string; shape: 'box' | 'decision' | 'start'; mark: Mark; vertical: boolean }
interface GroupData extends Record<string, unknown> { label: string }

const size = (label: string, shape: string) => {
  if (shape === 'start') return { width: 26, height: 26 }
  const lines = wrap(label)
  const longest = Math.max(...lines.map((l) => l.length))
  const pad = shape === 'decision' ? 54 : 34
  return { width: Math.min(280, Math.max(110, longest * 7.4 + pad)), height: lines.length * 17 + (shape === 'decision' ? 34 : 24) }
}

function Step({ data }: NodeProps<Node<StepData>>) {
  const t = data.vertical ? Position.Top : Position.Left
  const s = data.vertical ? Position.Bottom : Position.Right
  return (
    <div className={`vk-node ${data.shape} ${data.mark}`}>
      <Handle type="target" position={t} isConnectable={false} />
      {data.shape === 'start' ? null : wrap(data.label).map((l, i) => <div key={i}>{l || ' '}</div>)}
      <Handle type="source" position={s} isConnectable={false} />
    </div>
  )
}
function Group({ data }: NodeProps<Node<GroupData>>) { return <div className="vk-group"><span>{data.label}</span></div> }
const nodeTypes = { step: Step, group: Group }

interface Laid { nodes: Node[]; edges: { id: string; from: string; to: string; label?: string }[]; bounds: { w: number; h: number } }

function layout(flow: Flow): Laid {
  const g = new dagre.graphlib.Graph({ compound: true, multigraph: true })
  g.setGraph({ rankdir: flow.dir === 'LR' ? 'LR' : 'TB', nodesep: 36, ranksep: 54, marginx: 16, marginy: 16 })
  g.setDefaultEdgeLabel(() => ({}))
  for (const grp of flow.groups) g.setNode(grp.id, {})
  const dims = new Map(flow.nodes.map((n) => [n.id, size(n.label, n.shape)]))
  for (const n of flow.nodes) { g.setNode(n.id, dims.get(n.id)!); if (n.group) g.setParent(n.id, n.group) }
  const edges = flow.edges.map((e, i) => ({ ...e, id: `e${i}` }))
  for (const e of edges) g.setEdge(e.from, e.to, e.label ? { width: Math.min(150, e.label.length * 6.5 + 12), height: 18, labelpos: 'c' } : {}, e.id)
  dagre.layout(g)

  const topLeft = (id: string) => { const n = g.node(id); return { x: n.x - n.width / 2, y: n.y - n.height / 2, w: n.width, h: n.height } }
  const nodes: Node[] = []
  for (const grp of flow.groups) {
    const p = topLeft(grp.id)
    nodes.push({ id: grp.id, type: 'group', position: { x: p.x, y: p.y }, data: { label: grp.label }, style: { width: p.w, height: p.h }, selectable: false, draggable: false, zIndex: -1 })
  }
  for (const n of flow.nodes) {
    const p = topLeft(n.id)
    const parent = n.group ? topLeft(n.group) : null
    nodes.push({
      id: n.id, type: 'step', position: { x: p.x - (parent?.x ?? 0), y: p.y - (parent?.y ?? 0) }, parentId: n.group, draggable: false,
      data: { label: n.label, shape: n.shape, mark: 'idle', vertical: flow.dir === 'TD' } satisfies StepData, width: p.w, height: p.h,
    })
  }
  const bounds = g.graph()
  return { nodes, edges, bounds: { w: bounds.width ?? 600, h: bounds.height ?? 400 } }
}

function reach(start: string, next: Map<string, string[]>) {
  const seen = new Set([start])
  const todo = [start]
  while (todo.length) for (const n of next.get(todo.pop()!) ?? []) if (!seen.has(n)) { seen.add(n); todo.push(n) }
  return seen
}

function Body({ flow, fullscreen }: { flow: Flow; fullscreen: boolean }) {
  const laid = useMemo(() => layout(flow), [flow])
  const rf = useReactFlow()
  const [focus, setFocus] = useState<string | null>(null)
  const [walk, setWalk] = useState<string[] | null>(null)

  const out = useMemo(() => { const m = new Map<string, { to: string; label?: string; id: string }[]>(); for (const e of laid.edges) m.set(e.from, [...(m.get(e.from) ?? []), { to: e.to, label: e.label, id: e.id }]); return m }, [laid])
  const first = useMemo(() => flow.nodes.find((n) => !flow.edges.some((e) => e.to === n.id))?.id ?? flow.nodes[0].id, [flow])
  const label = (id: string) => flow.nodes.find((n) => n.id === id)?.label.replace(/\n/g, ' ') ?? id

  const marks = useMemo(() => {
    const nodeMark = new Map<string, Mark>()
    const edgeMark = new Map<string, Mark>()
    if (walk) {
      const done = new Set(walk)
      const current = walk[walk.length - 1]
      for (const n of flow.nodes) nodeMark.set(n.id, n.id === current ? 'current' : done.has(n.id) ? 'past' : 'future')
      for (let i = 1; i < walk.length; i++) for (const e of laid.edges) if (e.from === walk[i - 1] && e.to === walk[i]) edgeMark.set(e.id, 'past')
      for (const o of out.get(current) ?? []) edgeMark.set(o.id, 'current')
    } else if (focus) {
      const down = reach(focus, new Map([...out].map(([k, v]) => [k, v.map((x) => x.to)])))
      const back = new Map<string, string[]>()
      for (const e of laid.edges) back.set(e.to, [...(back.get(e.to) ?? []), e.from])
      const up = reach(focus, back)
      for (const n of flow.nodes) nodeMark.set(n.id, n.id === focus ? 'current' : down.has(n.id) || up.has(n.id) ? 'on' : 'dim')
      for (const e of laid.edges) edgeMark.set(e.id, (down.has(e.from) && down.has(e.to)) || (up.has(e.from) && up.has(e.to)) ? 'on' : 'dim')
    }
    return { nodeMark, edgeMark }
  }, [walk, focus, flow, laid, out])

  const nodes = useMemo(() => laid.nodes.map((n) => (n.type === 'step' ? { ...n, data: { ...n.data, mark: marks.nodeMark.get(n.id) ?? 'idle' } } : n)), [laid, marks])
  const edges: Edge[] = useMemo(() => laid.edges.map((e) => {
    const mark = marks.edgeMark.get(e.id) ?? 'idle'
    const color = mark === 'current' ? '#34f5a2' : mark === 'past' ? '#22d3ee' : mark === 'on' ? '#22d3ee' : '#4b6089'
    return {
      id: e.id, source: e.from, target: e.to, type: 'smoothstep', label: e.label, animated: mark === 'current' || mark === 'on',
      style: { stroke: color, strokeWidth: mark === 'dim' ? 1 : 1.8, opacity: mark === 'dim' || (walk && mark === 'idle') ? 0.25 : 1 },
      markerEnd: { type: MarkerType.ArrowClosed, color }, labelStyle: { fill: '#e6edf8', fontSize: 11, fontWeight: 600 }, labelBgStyle: { fill: '#0e1628', fillOpacity: 0.95 }, labelBgPadding: [5, 3], labelBgBorderRadius: 5,
    } satisfies Edge
  }), [laid, marks, walk])

  useEffect(() => { const t = setTimeout(() => void rf.fitView({ padding: 0.12, duration: 300 }), 50); return () => clearTimeout(t) }, [rf, fullscreen, laid])

  const current = walk?.[walk.length - 1]
  const choices = current ? out.get(current) ?? [] : []
  const height = fullscreen ? undefined : Math.min(560, Math.max(260, laid.bounds.h * 0.9 + 40))

  return (
    <>
      <div className="vk-canvas" style={{ height }}>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.15} maxZoom={1.8} nodesDraggable={false} nodesConnectable={false}
          zoomOnScroll={false} preventScrolling={false} colorMode="dark" onNodeClick={(_, n) => {
            if (n.type !== 'step') return
            if (walk) setWalk((w) => (w && w.includes(n.id) ? w.slice(0, w.indexOf(n.id) + 1) : [...(w ?? []), n.id]))
            else setFocus((f) => (f === n.id ? null : n.id))
          }} onPaneClick={() => setFocus(null)}>
          <Background gap={20} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <div className="vk-bar">
        {!walk ? (
          <>
            <button className="small" onClick={() => { setFocus(null); setWalk([first]) }}>▶ Walk through</button>
            <small className="muted">{focus ? <>Showing every step that leads to or follows <b>{label(focus)}</b>. Click it again to reset.</> : 'Click a step to trace its path, or walk through the flow one decision at a time.'}</small>
          </>
        ) : (
          <>
            <span className="vk-now"><small className="muted">You are at</small> <b>{label(current!)}</b></span>
            {choices.length === 0 && <small className="ok">End of this path.</small>}
            {choices.map((c) => <button key={c.id} className="small ghost" onClick={() => setWalk([...walk, c.to])}>{c.label ? `${c.label} → ` : '→ '}{label(c.to).slice(0, 38)}</button>)}
            <span className="spacer" />
            {walk.length > 1 && <button className="small ghost" onClick={() => setWalk(walk.slice(0, -1))}>◀ Back</button>}
            <button className="small ghost" onClick={() => setWalk([first])}>↺ Restart</button>
            <button className="small ghost" onClick={() => setWalk(null)}>✕ Exit</button>
          </>
        )}
      </div>
    </>
  )
}

export default function FlowDiagram({ flow, title }: { flow: Flow; title: string }) {
  return (
    <Frame title={title} kind="Flow diagram">
      {(fullscreen) => <ReactFlowProvider><Body flow={flow} fullscreen={fullscreen} /></ReactFlowProvider>}
    </Frame>
  )
}
