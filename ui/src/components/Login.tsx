import { useState, type FormEvent } from 'react'
import { api } from '../api'

export default function Login({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await api('/api/login', { method: 'POST', body: { token } })
      onLoggedIn()
    } catch (err) {
      setError((err as Error).message)
    }
    setBusy(false)
  }

  return (
    <div className="main" style={{ display: 'grid', placeItems: 'center', minHeight: '100vh' }}>
      <form className="card form" onSubmit={submit} style={{ width: 380 }}>
        <div className="brand" style={{ marginBottom: 8 }}><span className="brand-mark">⚡</span><div><b>OCPP SIM</b><small>1.6 &amp; 2.0.1 charger simulator</small></div></div>
        <label>Access token
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="the SIM_TOKEN this server was started with" autoFocus autoComplete="current-password" />
        </label>
        {error && <p className="error">{error}</p>}
        <div className="row"><button type="submit" disabled={busy || !token}>{busy ? 'Checking…' : 'Unlock'}</button></div>
      </form>
    </div>
  )
}
