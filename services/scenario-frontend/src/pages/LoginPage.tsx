import { useState } from 'react'
import { login } from '../api/client'
import type { Session } from '../types'

interface Props {
  onLoggedIn: (session: Session) => void
}

export default function LoginPage({ onLoggedIn }: Props) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      const session = await login(email, password)
      onLoggedIn(session)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-screen">
      <div className="login-form-panel">
        <form className="login-form" onSubmit={handleSubmit}>
          <h2>Welcome to DISA.AI</h2>

          {error && <div className="banner error">{error}</div>}

          <label>
            Email
            <input
              type="email"
              placeholder="Key in Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              required
            />
          </label>

          <label>
            Password
            <div className="password-field">
              <input
                type={showPassword ? 'text' : 'password'}
                placeholder="Key in Password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
              <button
                type="button"
                className="password-toggle"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? '🙈' : '👁'}
              </button>
            </div>
          </label>

          <button type="submit" className="login-submit" disabled={loading}>
            {loading ? 'Logging in…' : 'Login'}
          </button>
        </form>
      </div>

      <div className="login-footer">
        © 2026 - Global Innovate Corp: berizin dan diawasi oleh Otoritas Jasa Keuangan dan Bank Indonesia serta
        merupakan peserta penjaminan Lembaga Penjaminan Simpanan.
      </div>
    </div>
  )
}
