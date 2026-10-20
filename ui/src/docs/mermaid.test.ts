import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { parseMermaid } from './mermaid'

const root = join(__dirname, '../../../docs/guide')
const files = [...readdirSync(root).filter((f) => f.endsWith('.md')).map((f) => join(root, f)), ...readdirSync(join(root, 'reference')).map((f) => join(root, 'reference', f))]
const blocks = files.flatMap((f) => [...readFileSync(f, 'utf8').matchAll(/```mermaid\n([\s\S]*?)```/g)].map((m, i) => ({ name: `${f.slice(root.length + 1)} #${i + 1}`, src: m[1] })))

describe('guide diagrams', () => {
  it('finds the diagrams', () => expect(blocks.length).toBeGreaterThan(30))
  for (const b of blocks) {
    it(`parses ${b.name}`, () => {
      const d = parseMermaid(b.src)
      if (d.kind === 'flow') {
        const ids = new Set(d.nodes.map((n) => n.id))
        expect(d.nodes.length).toBeGreaterThan(1)
        for (const e of d.edges) { expect(ids.has(e.from)).toBe(true); expect(ids.has(e.to)).toBe(true) }
        for (const n of d.nodes) if (n.group) expect(d.groups.some((g) => g.id === n.group)).toBe(true)
      } else {
        expect(d.participants.length).toBeGreaterThan(1)
        for (const p of d.participants) expect(p.id).toMatch(/^\w+$/)
        for (const i of d.items) if (i.t === 'msg') expect(d.participants.some((p) => p.id === i.from) && d.participants.some((p) => p.id === i.to)).toBe(true)
        expect(d.items.filter((i) => i.t === 'msg').length).toBeGreaterThan(0)
        const depth = d.items.reduce((n, i) => n + (i.t === 'block' && i.kind !== 'else' && i.kind !== 'and' ? 1 : i.t === 'end' ? -1 : 0), 0)
        expect(depth).toBe(0)
      }
    })
  }
})
