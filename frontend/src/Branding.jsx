import React from 'react'


export function LoginBrand() {
  return (
    <div className="login-brand-lockup">
      <div className="login-brand-viewport">
        <img
          src="/branding/dentsu-sitemind-lockup.png"
          alt="Dentsu SiteMind"
          className="login-brand-image"
        />
      </div>
    </div>
  )
}


export function SidebarBrand() {
  return (
    <div className="sitemind-sidebar-brand">
      <div className="sitemind-sidebar-logo-shell">
        <img
          src="/branding/dentsu-sitemind-lockup.png"
          alt=""
          className="sitemind-sidebar-logo"
        />
      </div>

      <div className="sitemind-sidebar-brand-copy">
        <strong>
          SiteMind
        </strong>

        <span>
          SEO Intelligence Workspace
        </span>
      </div>
    </div>
  )
}


export function DentsuTopBrand() {
  return (
    <div
      className="topbar-dentsu"
      aria-label="Dentsu"
    >
      <span
        className="topbar-brand-line"
        aria-hidden="true"
      />

      <div className="topbar-dentsu-logo-frame">
        <img
          src="/branding/dentsu-wordmark.png"
          alt="dentsu"
          className="topbar-dentsu-logo"
        />
      </div>

      <span
        className="topbar-brand-line"
        aria-hidden="true"
      />
    </div>
  )
}
