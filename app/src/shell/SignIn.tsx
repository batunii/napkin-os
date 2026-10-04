// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Signing in to the studio (napkin-web's /api/auth/*, crates/napkin-web/src/auth.rs).
//
// A server with accounts answers /api/session with 401 {signin: true} until a
// person signs in as `name@agency`. The first sign-in with the temporary
// password an admin gave them asks for their own password, then the studio
// opens. The gate renders the studio unchanged when the server has no
// accounts (anonymous mode), when the page is the public viewer, on the
// device and on the desktop.

import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { StudioLogo } from '../brand/StudioMark'
import { LogoSpinnerFill } from '../brand/LogoSpinner'
import { PoweredByClan } from '../brand/PoweredByClan'
import './SignIn.css'

type Gate = 'checking' | 'open' | 'signin'

export function SignInGate({ children, check }: { children: ReactNode; check: boolean }) {
  const [gate, setGate] = useState<Gate>(check ? 'checking' : 'open')
  useEffect(() => {
    if (!check) return
    let live = true
    fetch('/api/session', { credentials: 'same-origin' })
      .then(async r => {
        if (!live) return
        if (r.status === 401) {
          const b = await r.json().catch(() => ({}))
          setGate(b && b.signin ? 'signin' : 'open')
        } else setGate('open')
      })
      // offline or no server: the studio says so itself
      .catch(() => { if (live) setGate('open') })
    return () => { live = false }
  }, [check])
  if (gate === 'checking') return <LogoSpinnerFill label="Opening the studio…" />
  if (gate === 'signin') return <SignIn onDone={() => location.reload()} />
  return <>{children}</>
}

async function post(path: string, body: unknown): Promise<{ status: number; data: Record<string, unknown> }> {
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return { status: r.status, data: await r.json().catch(() => ({})) }
}

export async function signOut() {
  await fetch('/api/auth/sign-out', { method: 'POST', credentials: 'same-origin' }).catch(() => {})
  location.reload()
}

function SignIn({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [fresh, setFresh] = useState('')
  const [again, setAgain] = useState('')
  const [challenge, setChallenge] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const signIn = async (e: FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !password) { setError('Enter your sign-in name and password.'); return }
    setBusy(true); setError(null)
    try {
      const { status, data } = await post('/api/auth/sign-in', { username: username.trim(), password })
      if (data.ok) { onDone(); return }
      if (data.step === 'new_password' && typeof data.session === 'string') { setChallenge(data.session); setPassword(''); return }
      setError(typeof data.error === 'string' ? data.error : `Sign-in failed (${status}).`)
    } catch {
      setError('The studio could not be reached. Try again.')
    } finally { setBusy(false) }
  }

  const choose = async (e: FormEvent) => {
    e.preventDefault()
    if (fresh !== again) { setError('The two passwords are not the same.'); return }
    setBusy(true); setError(null)
    try {
      const { status, data } = await post('/api/auth/new-password', { username: username.trim(), session: challenge, password: fresh })
      if (data.ok) { onDone(); return }
      setError(typeof data.error === 'string' ? data.error : `That did not work (${status}).`)
      if (typeof data.error === 'string' && /sign in again/i.test(data.error)) { setChallenge(null); setFresh(''); setAgain('') }
    } catch {
      setError('The studio could not be reached. Try again.')
    } finally { setBusy(false) }
  }

  return (
    <main className="si">
      <div className="si-card">
        <div className="si-mark"><StudioLogo size={20} /></div>
        {challenge === null ? (
          <form onSubmit={signIn} noValidate>
            <h1 className="si-h">Sign in to the studio</h1>
            <label className="si-f">
              <span>Sign-in name</span>
              <input name="username" autoComplete="username" autoCapitalize="none" spellCheck={false}
                placeholder="name@agency" value={username} onChange={e => setUsername(e.target.value)} autoFocus />
            </label>
            <label className="si-f">
              <span>Password</span>
              <input name="password" type="password" autoComplete="current-password"
                value={password} onChange={e => setPassword(e.target.value)} />
            </label>
            {error && <p className="si-err" role="alert">{error}</p>}
            <button className="si-go" type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
            <p className="si-note">Your sign-in name and first password come from your studio admin.</p>
          </form>
        ) : (
          <form onSubmit={choose} noValidate>
            <h1 className="si-h">Choose your password</h1>
            <p className="si-lede">This is your first sign-in as <b>{username.trim().toLowerCase()}</b>. Pick a password only you know: at least 10 characters, with a letter and a number.</p>
            <label className="si-f">
              <span>New password</span>
              <input type="password" autoComplete="new-password" value={fresh} onChange={e => setFresh(e.target.value)} autoFocus />
            </label>
            <label className="si-f">
              <span>The same again</span>
              <input type="password" autoComplete="new-password" value={again} onChange={e => setAgain(e.target.value)} />
            </label>
            {error && <p className="si-err" role="alert">{error}</p>}
            <button className="si-go" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save and open the studio'}</button>
          </form>
        )}
      </div>
      <footer className="si-foot"><PoweredByClan /></footer>
    </main>
  )
}
