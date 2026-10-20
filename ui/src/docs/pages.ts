/** The guide pages, bundled from docs/guide at build time so the web UI needs no extra server route. */
const raw = import.meta.glob('../../../docs/guide/**/*.md', { query: '?raw', import: 'default', eager: true }) as Record<string, string>

export interface Section { id: string; level: number; title: string; text: string }
export interface Page { key: string; title: string; group: 'Guide' | 'Reference'; markdown: string; sections: Section[] }

/** GitHub's heading anchor, the same rule the guide's links were written against. */
export function slug(heading: string): string {
  const text = heading.trim().toLowerCase().replace(/[`*_]/g, '')
  return text.replace(/[^\w\s-]/g, '').replace(/\s/g, '-')
}

function sections(markdown: string): Section[] {
  const out: Section[] = []
  let fence = false
  let cur: Section | null = null
  for (const line of markdown.split('\n')) {
    if (line.startsWith('```')) fence = !fence
    const m = !fence && line.match(/^(#{1,3})\s+(.*)/)
    if (m) { cur = { id: slug(m[2]), level: m[1].length, title: m[2].replace(/[`*]/g, ''), text: '' }; out.push(cur) } else if (cur) cur.text += line + ' '
  }
  return out
}

const prefix = '../../../docs/guide/'
export const PAGES: Page[] = Object.entries(raw)
  .map(([path, markdown]) => {
    const key = path.slice(prefix.length).replace(/\.md$/, '')
    const s = sections(markdown)
    return { key, title: s[0]?.title ?? key, markdown, sections: s, group: key.startsWith('reference/') ? 'Reference' as const : 'Guide' as const }
  })
  .sort((a, b) => (a.group === b.group ? (a.key === 'README' ? -1 : b.key === 'README' ? 1 : a.key.localeCompare(b.key)) : a.group === 'Guide' ? -1 : 1))

export const PAGE_BY_KEY = new Map(PAGES.map((p) => [p.key, p]))
export const HOME = 'README'

/** Resolve a markdown link written inside page `from` to a page key and optional anchor. */
export function resolveLink(from: string, href: string): { page: string; anchor?: string } | null {
  const [file, anchor] = href.split('#')
  if (!file) return { page: from, anchor }
  const dir = from.includes('/') ? from.slice(0, from.lastIndexOf('/') + 1) : ''
  const parts = (dir + file.replace(/\.md$/, '')).split('/')
  const stack: string[] = []
  for (const p of parts) { if (p === '..') stack.pop(); else if (p !== '.') stack.push(p) }
  const key = stack.join('/')
  return PAGE_BY_KEY.has(key) ? { page: key, anchor } : null
}

export function search(query: string, limit = 24): { page: Page; section: Section; snippet: string }[] {
  const terms = query.toLowerCase().split(/\s+/).filter((t) => t.length > 1)
  if (!terms.length) return []
  const hits: { page: Page; section: Section; snippet: string; score: number }[] = []
  for (const page of PAGES) for (const section of page.sections) {
    const title = section.title.toLowerCase()
    const body = section.text.toLowerCase()
    if (!terms.every((t) => title.includes(t) || body.includes(t))) continue
    const plain = section.text.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/[|`*#]/g, '')
    const found = plain.toLowerCase().indexOf(terms[0])
    const snippet = found < 0 ? plain.slice(0, 120) : (found > 50 ? '…' : '') + plain.slice(Math.max(0, found - 50), found + 110)
    hits.push({ page, section, snippet, score: terms.reduce((n, t) => n + (title.includes(t) ? 5 : 0) + (body.split(t).length - 1 > 0 ? 1 : 0), 0) })
  }
  return hits.sort((a, b) => b.score - a.score).slice(0, limit)
}
