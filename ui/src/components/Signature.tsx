import { ABOUT } from '../about'

/** The owner's signature, shown in the sidebar, on the login screen and under every page. */
export default function Signature({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return <div className="footer-sig" data-owner={ABOUT.github}>{ABOUT.signature} · <a href={ABOUT.repository} target="_blank" rel="noreferrer">github.com/{ABOUT.github}</a><br />{ABOUT.copyright}</div>
  }
  return (
    <div className="signature" data-owner={ABOUT.github}>
      <b>{ABOUT.name}</b> · {ABOUT.tagline}<br />
      {ABOUT.signature}<br />
      <a href={ABOUT.repository} target="_blank" rel="noreferrer">github.com/{ABOUT.github}</a><br />
      {ABOUT.copyright}
    </div>
  )
}
