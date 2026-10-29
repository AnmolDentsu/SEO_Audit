import React, {
  useEffect,
  useState,
} from 'react'

import {
  getCurrentUser,
  logout,
  startLogin,
} from './auth.js'


function initials(user) {
  const source =
    user?.name ||
    user?.email ||
    'SiteMind'

  return source
    .split(/[\s.@_-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(value => value[0]?.toUpperCase())
    .join('')
}


export default function AuthGate({
  children,
}) {
  const [loading, setLoading] =
    useState(true)

  const [authState, setAuthState] =
    useState({
      authenticated: false,
      user: null,
    })

  const [error, setError] =
    useState('')

  const [loggingOut, setLoggingOut] =
    useState(false)


  useEffect(() => {
    let mounted = true

    async function bootstrap() {
      try {
        const state =
          await getCurrentUser()

        if (!mounted) {
          return
        }

        setAuthState(state)
        setError('')

      } catch (err) {
        if (!mounted) {
          return
        }

        setError(
          err?.message ||
          'Unable to verify your SiteMind session.'
        )

        setAuthState({
          authenticated: false,
          user: null,
        })

      } finally {
        if (mounted) {
          setLoading(false)
        }
      }
    }

    bootstrap()

    return () => {
      mounted = false
    }
  }, [])


  async function handleLogout() {
    if (loggingOut) {
      return
    }

    setLoggingOut(true)
    setError('')

    try {
      await logout()

    } catch (err) {
      setError(
        err?.message ||
        'Unable to sign out.'
      )

      setLoggingOut(false)
    }
  }


  if (loading) {
    return (
      <div className="auth-screen">
        <div className="auth-card compact">
          <div className="auth-brand-mark">
            S
          </div>

          <h1>
            SiteMind
          </h1>

          <p>
            Verifying your secure session…
          </p>

          <div className="auth-loader" />
        </div>
      </div>
    )
  }


  if (!authState.authenticated) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <div className="auth-brand-row">
            <div className="auth-brand-mark">
              S
            </div>

            <div>
              <strong>
                SiteMind
              </strong>

              <span>
                SEO AI Platform
              </span>
            </div>
          </div>

          <h1>
            Sign in to SiteMind
          </h1>

          <p>
            Access is restricted to approved Dentsu users.
            Continue with your corporate Okta account.
          </p>

          {error && (
            <div className="auth-error">
              {error}
            </div>
          )}

          <button
            type="button"
            className="auth-login-button"
            onClick={startLogin}
          >
            Continue with Dentsu Okta
          </button>

          <small>
            SiteMind uses a shared workspace. Projects,
            runs and analysis outputs are visible to other
            approved SiteMind users.
          </small>
        </div>
      </div>
    )
  }


  const user =
    authState.user || {}


  return (
    <>
      {children}

      <div className="auth-user-chip">
        <div className="auth-user-avatar">
          {initials(user)}
        </div>

        <div className="auth-user-copy">
          <strong>
            {user.name || 'SiteMind user'}
          </strong>

          <span>
            {user.email}
          </span>
        </div>

        <button
          type="button"
          onClick={handleLogout}
          disabled={loggingOut}
        >
          {loggingOut
            ? 'Signing out…'
            : 'Sign out'}
        </button>
      </div>

      {error && (
        <div className="auth-floating-error">
          {error}
        </div>
      )}
    </>
  )
}