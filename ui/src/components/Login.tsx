import { useState, type FormEvent } from 'react'
import { api } from '../api'
import Signature from './Signature'

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
        <div className="brand" style={{ marginBottom: 8 }}><img className="brand-logo" src="/logo.svg" alt="Volkai logo" width={44} height={44} /><div><b>VOLKAI</b><small>OCPP 1.6 · 2.0.1</small></div></div>
        <label>Access token
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="the SIM_TOKEN this server was started with" autoFocus autoComplete="current-password" />
        </label>
        {error && <p className="error">{error}</p>}
        <div className="row"><button type="submit" disabled={busy || !token}>{busy ? 'Checking…' : 'Unlock'}</button></div>
      </form>
      <Signature compact />
    </div>
  )
}
