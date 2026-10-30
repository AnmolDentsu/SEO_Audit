import React, {
  useEffect,
  useState,
} from 'react'

import {
  getCurrentUser,
  logout,
  startLogin,
} from './auth.js'

import {
  LoginBrand,
} from './Branding.jsx'

import ThemeToggle from './ThemeToggle.jsx'


function initials(user) {
  const source =
    user?.name ||
    user?.email ||
    'SiteMind'

  return source
    .split(/[\s.@_-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(value =>
      value[0]?.toUpperCase()
    )
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
      <div className="auth-screen auth-screen-loading">
        <ThemeToggle className="auth-theme-toggle" />

        <div className="auth-card auth-card-loading">
          <LoginBrand />

          <div className="auth-loading-copy">
            <strong>
              Loading SiteMind
            </strong>

            <span>
              Verifying your secure session…
            </span>
          </div>

          <div className="auth-loader" />
        </div>
      </div>
    )
  }


  if (!authState.authenticated) {
    return (
      <div className="auth-screen">
        <ThemeToggle className="auth-theme-toggle" />

        <div className="auth-background-orb auth-orb-one" />
        <div className="auth-background-orb auth-orb-two" />
        <div className="auth-background-orb auth-orb-three" />

        <div className="auth-card auth-login-card">
          <LoginBrand />

          <div className="auth-security-pill">
            <span className="auth-security-dot" />
            Secure enterprise access
          </div>

          <h1>
            Welcome to SiteMind
          </h1>

          <p className="auth-intro">
            AI-assisted SEO intelligence for
            public-web analysis, benchmarking
            and sitemap discovery.
          </p>

          <p className="auth-supporting-copy">
            Sign in using your approved Dentsu
            corporate Okta account to continue.
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
            <span>
              Continue with Dentsu Okta
            </span>

            <span
              className="auth-login-arrow"
              aria-hidden="true"
            >
              →
            </span>
          </button>

          <div className="auth-trust-row">
            <span>
              Approved users only
            </span>

            <i />

            <span>
              Shared workspace
            </span>

            <i />

            <span>
              Audited actions
            </span>
          </div>
        </div>

        <div className="auth-footer">
          SiteMind · SEO AI Platform
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