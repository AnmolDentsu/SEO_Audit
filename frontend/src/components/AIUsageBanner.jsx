import React, {
  useEffect,
  useState,
} from 'react'

import {
  getPublicDataConsent,
  setPublicDataConsent,
} from '../compliance.js'


export default function AIUsageBanner() {
  const [confirmed, setConfirmed] =
    useState(
      getPublicDataConsent()
    )

  useEffect(() => {
    const handler = event => {
      setConfirmed(
        Boolean(
          event.detail?.confirmed
        )
      )
    }

    window.addEventListener(
      'seo-public-data-consent-changed',
      handler
    )

    return () => {
      window.removeEventListener(
        'seo-public-data-consent-changed',
        handler
      )
    }
  }, [])


  const change = value => {
    setConfirmed(value)
    setPublicDataConsent(value)
  }


  return (
    <section className="ai-usage-banner">
      <div className="ai-banner-title">
        <div>
          <strong>
            AI-assisted public-web SEO analysis
          </strong>

          <p>
            AI-generated classifications and recommendations
            are advisory and must be human-reviewed before
            implementation or client delivery.
          </p>
        </div>

        <span className="ai-badge">
          AI model
        </span>
      </div>

      <div className="ai-scope-grid">
        <div className="ai-scope allowed">
          <strong>
            Permitted
          </strong>

          <span>
            ✓ Public website URLs
          </span>

          <span>
            ✓ Public sitemap XML
          </span>

          <span>
            ✓ Public HTML/text and metadata
          </span>

          <span>
            ✓ SEO keywords
          </span>

          <span>
            ✓ Approved search-volume / SEO metrics
          </span>

          <span>
            ✓ Non-sensitive project labels
          </span>
        </div>

        <div className="ai-scope prohibited">
          <strong>
            Do not submit
          </strong>

          <span>
            ✕ Personal data / PII
          </span>

          <span>
            ✕ Payment information
          </span>

          <span>
            ✕ Passwords, tokens, credentials or cookies
          </span>

          <span>
            ✕ Dentsu Secret / restricted information
          </span>

          <span>
            ✕ Unapproved client-confidential data
          </span>

          <span>
            ✕ Internal/authenticated webpages or databases
          </span>
        </div>
      </div>

      <label className="ai-compliance-confirmation">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={event =>
            change(
              event.target.checked
            )
          }
        />

        <span>
          I confirm that the inputs used in this session
          are permitted public/non-sensitive SEO data and
          that I am authorised to analyse them.
        </span>
      </label>

      {!confirmed && (
        <div className="ai-compliance-warning">
          Confirmation is required before a Page or
          Sitemap analysis can be started.
        </div>
      )}
    </section>
  )
}