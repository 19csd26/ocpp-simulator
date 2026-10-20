/** Reads the Mermaid subset used by the guide (flowchart, sequenceDiagram, stateDiagram-v2) into plain data that React Flow draws. */

export interface FlowNode { id: string; label: string; shape: 'box' | 'decision' | 'start'; group?: string }
export interface FlowEdge { from: string; to: string; label?: string }
export interface FlowGroup { id: string; label: string }
export interface Flow { kind: 'flow'; dir: 'TD' | 'LR'; nodes: FlowNode[]; edges: FlowEdge[]; groups: FlowGroup[] }

export interface Participant { id: string; label: string }
export type SeqItem =
  | { t: 'msg'; from: string; to: string; text: string; dashed: boolean; lost: boolean }
  | { t: 'note'; over: string[]; side: 'over' | 'right' | 'left'; text: string }
  | { t: 'block'; kind: string; text: string }
  | { t: 'end' }
export interface Sequence { kind: 'seq'; participants: Participant[]; items: SeqItem[] }

export type Diagram = Flow | Sequence

const clean = (s: string) => s.replace(/<br\s*\/?>/gi, '\n').replace(/\\"/g, '"').trim()

const NODE = /([A-Za-z_][\w-]*)(?:\[\[?"([^"]*)"\]?\]|\{"([^"]*)"\}|\("([^"]*)"\)|\[([^\]"]*)\]|\{([^}"]*)\}|\(([^)"]*)\))?/y
const EDGE = /\s*(?:-->|---|==>|-\.->)(?:\|"?([^|"]*)"?\|)?\s*/y

function parseFlow(lines: string[], dir: 'TD' | 'LR'): Flow {
  const nodes = new Map<string, FlowNode>()
  const edges: FlowEdge[] = []
  const groups: FlowGroup[] = []
  const stack: string[] = []
  const touch = (id: string, label?: string, shape?: FlowNode['shape']) => {
    const known = nodes.get(id)
    if (!known) nodes.set(id, { id, label: label ?? id, shape: shape ?? 'box', group: stack[stack.length - 1] })
    else { if (label !== undefined) known.label = label; if (shape) known.shape = shape }
  }
  for (const raw of lines) {
    const line = raw.trim()
    if (!line || line.startsWith('%%') || /^direction\s/.test(line)) continue
    const sub = line.match(/^subgraph\s+([\w-]+)(?:\["([^"]*)"\])?/)
    if (sub) { groups.push({ id: sub[1], label: clean(sub[2] ?? sub[1]) }); stack.push(sub[1]); continue }
    if (line === 'end') { stack.pop(); continue }
    let pos = 0
    let prev: string | null = null
    let label: string | undefined
    while (pos < line.length) {
      NODE.lastIndex = pos
      const m = NODE.exec(line)
      if (!m) throw new Error(`Cannot read: ${line}`)
      const id = m[1]
      const text = m[2] ?? m[3] ?? m[4] ?? m[5] ?? m[6] ?? m[7]
      touch(id, text === undefined ? undefined : clean(text), m[3] !== undefined || m[6] !== undefined ? 'decision' : text === undefined ? undefined : 'box')
      if (prev) edges.push({ from: prev, to: id, label })
      prev = id
      pos = NODE.lastIndex
      if (pos >= line.length) break
      EDGE.lastIndex = pos
      const e = EDGE.exec(line)
      if (!e) throw new Error(`Cannot read: ${line}`)
      label = e[1] === undefined ? undefined : clean(e[1])
      pos = EDGE.lastIndex
    }
  }
  return { kind: 'flow', dir, nodes: [...nodes.values()], edges, groups }
}

function parseState(lines: string[]): Flow {
  const nodes = new Map<string, FlowNode>()
  const edges: FlowEdge[] = []
  const node = (id: string, shape: FlowNode['shape'] = 'box') => { if (!nodes.has(id)) nodes.set(id, { id, label: shape === 'start' ? 'start' : id, shape }) }
  for (const raw of lines) {
    const line = raw.trim()
    if (!line || line.startsWith('%%')) continue
    const m = line.match(/^(\[\*\]|[\w-]+)\s*-->\s*(\[\*\]|[\w-]+)\s*(?::\s*(.*))?$/)
    if (!m) throw new Error(`Cannot read: ${line}`)
    const [, a, b, text] = m
    const from = a === '[*]' ? '__start' : a
    const to = b === '[*]' ? '__end' : b
    node(from, a === '[*]' ? 'start' : 'box')
    node(to, b === '[*]' ? 'start' : 'box')
    edges.push({ from, to, label: text?.trim() })
  }
  return { kind: 'flow', dir: 'LR', nodes: [...nodes.values()], edges, groups: [] }
}

const MSG = /^(\w+)\s*(-->>|->>|--x|-x|-->|->)\s*(\w+)\s*:\s*(.*)$/
const NOTE = /^Note\s+(over|right of|left of)\s+(\w+)(?:\s*,\s*(\w+))?\s*:\s*(.*)$/i

function parseSequence(lines: string[]): Sequence {
  const participants: Participant[] = []
  const items: SeqItem[] = []
  const known = (id: string, label?: string) => {
    const p = participants.find((x) => x.id === id)
    if (!p) participants.push({ id, label: label ?? id })
    else if (label) p.label = label
  }
  for (const raw of lines) {
    const line = raw.trim()
    if (!line || line.startsWith('%%') || line === 'autonumber') continue
    const part = line.match(/^(?:participant|actor)\s+(\w+)(?:\s+as\s+(.*))?$/)
    if (part) { known(part[1], part[2]?.trim()); continue }
    const note = line.match(NOTE)
    if (note) {
      const over = [note[2], note[3]].filter(Boolean) as string[]
      over.forEach((id) => known(id))
      items.push({ t: 'note', over, side: note[1].toLowerCase().startsWith('right') ? 'right' : note[1].toLowerCase().startsWith('left') ? 'left' : 'over', text: clean(note[4]) })
      continue
    }
    const msg = line.match(MSG)
    if (msg) {
      known(msg[1]); known(msg[3])
      items.push({ t: 'msg', from: msg[1], to: msg[3], text: clean(msg[4]), dashed: msg[2].startsWith('--'), lost: msg[2].endsWith('x') })
      continue
    }
    const block = line.match(/^(loop|alt|opt|par|critical|break|else|and)\b\s*(.*)$/)
    if (block) { items.push({ t: 'block', kind: block[1], text: clean(block[2]) }); continue }
    if (line === 'end') { items.push({ t: 'end' }); continue }
    throw new Error(`Cannot read: ${line}`)
  }
  return { kind: 'seq', participants, items }
}

export function parseMermaid(source: string): Diagram {
  const lines = source.split('\n')
  const head = lines.findIndex((l) => l.trim())
  const first = (lines[head] ?? '').trim()
  const rest = lines.slice(head + 1)
  const flow = first.match(/^(?:flowchart|graph)\s*(TD|TB|LR|RL|BT)?/)
  if (flow) return parseFlow(rest, flow[1] === 'LR' || flow[1] === 'RL' ? 'LR' : 'TD')
  if (first === 'sequenceDiagram') return parseSequence(rest)
  if (first.startsWith('stateDiagram')) return parseState(rest)
  throw new Error(`Unsupported diagram type: ${first}`)
}
