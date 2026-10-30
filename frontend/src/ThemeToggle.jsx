import React, {
  useEffect,
  useState,
} from 'react'


const THEME_STORAGE_KEY =
  'sitemind-theme'


function detectInitialTheme() {
  if (typeof window === 'undefined') {
    return 'light'
  }

  const saved =
    window.localStorage.getItem(
      THEME_STORAGE_KEY
    )

  if (
    saved === 'light' ||
    saved === 'dark'
  ) {
    return saved
  }

  if (
    window.matchMedia?.(
      '(prefers-color-scheme: dark)'
    ).matches
  ) {
    return 'dark'
  }

  return 'light'
}


function applyTheme(theme) {
  document.documentElement.dataset.theme =
    theme

  document.documentElement.style.colorScheme =
    theme
}


export function initializeTheme() {
  const theme =
    detectInitialTheme()

  applyTheme(theme)

  return theme
}


export default function ThemeToggle({
  className = '',
}) {
  const [theme, setTheme] =
    useState(() => detectInitialTheme())


  useEffect(() => {
    applyTheme(theme)

    window.localStorage.setItem(
      THEME_STORAGE_KEY,
      theme
    )
  }, [theme])


  function toggleTheme() {
    setTheme(current =>
      current === 'dark'
        ? 'light'
        : 'dark'
    )
  }


  const dark =
    theme === 'dark'


  return (
    <button
      type="button"
      className={`theme-toggle ${className}`}
      onClick={toggleTheme}
      aria-label={
        dark
          ? 'Switch to light mode'
          : 'Switch to dark mode'
      }
      title={
        dark
          ? 'Switch to light mode'
          : 'Switch to dark mode'
      }
    >
      <span
        className="theme-toggle-track"
        aria-hidden="true"
      >
        <span className="theme-toggle-sun">
          ☀
        </span>

        <span className="theme-toggle-moon">
          ☾
        </span>

        <span className="theme-toggle-thumb">
          {dark ? '☾' : '☀'}
        </span>
      </span>

      <span className="theme-toggle-label">
        {dark ? 'Dark' : 'Light'}
      </span>
    </button>
  )
}