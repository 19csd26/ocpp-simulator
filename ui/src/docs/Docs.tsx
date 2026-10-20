import { Children, isValidElement, memo, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import Diagram from './Diagram'
import { HOME, PAGES, PAGE_BY_KEY, resolveLink, search, slug, type Page } from './pages'
import { docsHash, type DocsRoute } from './route'
import './docs.css'

const textOf = (node: ReactNode): string => Children.toArray(node).map((c) => (typeof c === 'string' || typeof c === 'number' ? String(c) : isValidElement(c) ? textOf((c.props as { children?: ReactNode }).children) : '')).join('')

function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="vk-codewrap">
      <div className="vk-codebar"><small>{lang || 'text'}</small><button className="small ghost" onClick={() => { void navigator.clipboard?.writeText(code).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200) }) }}>{copied ? 'Copied' : 'Copy'}</button></div>
      <pre className="vk-code"><code>{code}</code></pre>
    </div>
  )
}

const Article = memo(function Article({ page, go }: { page: Page; go: (page: string, anchor?: string) => void }) {
  const goRef = useRef(go)
  goRef.current = go
  const pageKey = page.key
  // stable between renders: a new component type would remount (and reset) every diagram
  const components = useMemo(() => {
    let heading = page.title
      return {
      h1: ({ children }: { children?: ReactNode }) => { heading = textOf(children); return <h1 id={slug(textOf(children))}>{children}</h1> },
      h2: ({ children }: { children?: ReactNode }) => { heading = textOf(children); return <h2 id={slug(textOf(children))}>{children}</h2> },
      h3: ({ children }: { children?: ReactNode }) => { heading = textOf(children); return <h3 id={slug(textOf(children))}>{children}</h3> },
      a: ({ href = '', children }: { href?: string; children?: ReactNode }) => {
        if (/^(https?:|mailto:)/.test(href)) return <a href={href} target="_blank" rel="noreferrer">{children}</a>
        const to = resolveLink(pageKey, href)
        if (!to) return <a href={`https://github.com/19csd26/ocpp-simulator/blob/main/docs/guide/${href}`} target="_blank" rel="noreferrer">{children}</a>
        return <a href={docsHash(to.page, to.anchor)} onClick={(e) => { e.preventDefault(); goRef.current(to.page, to.anchor) }}>{children}</a>
      },
      table: ({ children }: { children?: ReactNode }) => <div className="vk-table"><table>{children}</table></div>,
      pre: ({ children }: { children?: ReactNode }) => {
        const code = Children.toArray(children)[0]
        if (isValidElement(code)) {
          const props = code.props as { className?: string; children?: ReactNode }
          const lang = /language-([\w-]+)/.exec(props.className ?? '')?.[1] ?? ''
          const text = textOf(props.children).replace(/\n$/, '')
          return lang === 'mermaid' ? <Diagram source={text} title={heading} /> : <CodeBlock code={text} lang={lang} />
        }
        return <pre>{children}</pre>
      },
    }
  }, [pageKey, page.title])
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{page.markdown}</ReactMarkdown>
})

export default function Docs({ route, go }: { route: DocsRoute; go: (page: string, anchor?: string) => void }) {
  const page = PAGE_BY_KEY.get(route.page) ?? PAGE_BY_KEY.get(HOME)!
  const [query, setQuery] = useState('')
  const content = useRef<HTMLDivElement>(null)
  const results = useMemo(() => search(query), [query])
  const [active, setActive] = useState<string | null>(null)

  // scroll to the requested section, or to the top of a new page
  useEffect(() => {
    const el = route.anchor ? document.getElementById(route.anchor) : null
    if (el) el.scrollIntoView({ block: 'start' }); else window.scrollTo({ top: 0 })
  }, [route.page, route.anchor])

  // highlight the section being read in the contents list
  useEffect(() => {
    const heads = [...(content.current?.querySelectorAll('h2[id], h3[id]') ?? [])]
    const obs = new IntersectionObserver((entries) => { const seen = entries.filter((e) => e.isIntersecting); if (seen.length) setActive(seen[0].target.id) }, { rootMargin: '-10% 0px -75% 0px' })
    heads.forEach((h) => obs.observe(h))
    return () => obs.disconnect()
  }, [route.page])

  const toc = page.sections.filter((s) => s.level === 2 || s.level === 3)
  const index = PAGES.findIndex((p) => p.key === page.key)
  return (
    <div className="vk-docs">
      <nav className="vk-nav" aria-label="Documentation">
        <input type="search" placeholder="Search the guide…" value={query} onChange={(e) => setQuery(e.target.value)} />
        {query.trim().length > 1 ? (
          <div className="vk-results">
            {results.length === 0 && <small className="muted">Nothing found for “{query}”.</small>}
            {results.map((r, i) => (
              <button key={i} className="vk-result" onClick={() => { setQuery(''); go(r.page.key, r.section.level > 1 ? r.section.id : undefined) }}>
                <b>{r.section.title}</b><small className="muted">{r.page.title.replace(/^#\s*/, '')}</small><small>{r.snippet}</small>
              </button>
            ))}
          </div>
        ) : (
          <>
            {(['Guide', 'Reference'] as const).map((group) => (
              <div key={group} className="vk-group-nav">
                <h4>{group === 'Guide' ? 'Feature guide' : 'Reference (generated from the code)'}</h4>
                {PAGES.filter((p) => p.group === group).map((p) => (
                  <div key={p.key}>
                    <a href={docsHash(p.key)} className={p.key === page.key ? 'active' : ''} onClick={(e) => { e.preventDefault(); go(p.key) }}>{p.key === HOME ? 'Guide home' : p.title}</a>
                    {p.key === page.key && toc.length > 0 && (
                      <div className="vk-toc">{toc.map((s) => <a key={s.id} href={docsHash(p.key, s.id)} className={`l${s.level} ${active === s.id ? 'active' : ''}`} onClick={(e) => { e.preventDefault(); go(p.key, s.id) }}>{s.title}</a>)}</div>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </>
        )}
      </nav>
      <article className="vk-article" ref={content}>
        <Article page={page} go={go} />
        <div className="vk-pager">
          {index > 0 ? <a href={docsHash(PAGES[index - 1].key)} onClick={(e) => { e.preventDefault(); go(PAGES[index - 1].key) }}>← {PAGES[index - 1].title}</a> : <span />}
          {index < PAGES.length - 1 && <a href={docsHash(PAGES[index + 1].key)} onClick={(e) => { e.preventDefault(); go(PAGES[index + 1].key) }}>{PAGES[index + 1].title} →</a>}
        </div>
      </article>
    </div>
  )
}
