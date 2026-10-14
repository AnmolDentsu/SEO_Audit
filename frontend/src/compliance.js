const BASE = (
  import.meta.env.VITE_API_BASE_URL ||
  'http://localhost:8000'
).replace(/\/$/, '')


const CONSENT_KEY =
  'seo_public_data_confirmed'


export function getPublicDataConsent() {
  return (
    window.sessionStorage.getItem(
      CONSENT_KEY
    ) === 'true'
  )
}


export function setPublicDataConsent(value) {
  window.sessionStorage.setItem(
    CONSENT_KEY,
    value ? 'true' : 'false'
  )

  window.dispatchEvent(
    new CustomEvent(
      'seo-public-data-consent-changed',
      {
        detail: {
          confirmed: Boolean(value),
        },
      }
    )
  )
}


async function readError(response) {
  try {
    const body = await response.json()

    if (typeof body?.detail === 'string') {
      return body.detail
    }

    if (Array.isArray(body?.detail)) {
      return body.detail
        .map(item => item?.msg || JSON.stringify(item))
        .join('; ')
    }

    return JSON.stringify(
      body?.detail ?? body
    )
  } catch {
    return `${response.status} ${response.statusText}`
  }
}


export async function getQueueStatus(
  kind,
  runId
) {
  const params = new URLSearchParams({
    kind,
  })

  if (runId != null) {
    params.set(
      'run_id',
      String(runId)
    )
  }

  const response = await fetch(
    `${BASE}/api/queue/status?${params.toString()}`,
    {
      credentials: 'same-origin',
    }
  )

  if (!response.ok) {
    throw new Error(
      await readError(response)
    )
  }

  return response.json()
}


export function complianceRunFields() {
  return {
    public_data_confirmed:
      getPublicDataConsent(),
  }
}