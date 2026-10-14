const BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')

function formatApiDetail(detail) {
  if (!detail) return ''

  if (typeof detail === 'string') return detail

  if (Array.isArray(detail)) {
    return detail
      .map(item => {
        if (typeof item === 'string') return item

        const loc = Array.isArray(item?.loc)
          ? item.loc.filter(x => x !== 'body').join('.')
          : ''

        return `${loc ? `${loc}: ` : ''}${
          item?.msg || item?.message || JSON.stringify(item)
        }`
      })
      .join('; ')
  }

  if (typeof detail === 'object') {
    if (detail.message) {
      const extra =
        Array.isArray(detail.errors) && detail.errors.length
          ? ` ${detail.errors
              .map(
                e =>
                  `${e.row ? `row ${e.row}: ` : ''}${
                    e.error || e.message || JSON.stringify(e)
                  }`
              )
              .join('; ')}`
          : ''

      return `${detail.message}${extra}`
    }

    return JSON.stringify(detail)
  }

  return String(detail)
}

async function request(path, options = {}) {
  const isFormData =
    typeof FormData !== 'undefined' &&
    options.body instanceof FormData

  const headers = {
    ...(options.headers || {}),
  }

  if (
    !isFormData &&
    options.body != null &&
    !headers['Content-Type']
  ) {
    headers['Content-Type'] = 'application/json'
  }

  const response = await fetch(`${BASE}${path}`, {
    ...options,
    headers,
  })

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`

    try {
      const body = await response.json()
      message = formatApiDetail(body.detail ?? body)
    } catch {
      // Keep HTTP status/statusText when body is not JSON.
    }

    throw new Error(message)
  }

  if (response.status === 204) {
    return null
  }

  const contentType = response.headers.get('content-type') || ''

  return contentType.includes('application/json')
    ? response.json()
    : response
}

export const api = {
  base: BASE,

  // ============================================================
  // AI
  // ============================================================

  aiStatus: () =>
    request('/api/ai/status'),

  testAi: () =>
    request('/api/ai/test', {
      method: 'POST',
    }),

  // ============================================================
  // PROJECTS
  // ============================================================

  listProjects: () =>
    request('/api/projects'),

  createProject: body =>
    request('/api/projects', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  getProject: id =>
    request(`/api/projects/${id}`),

  updateProject: (id, body) =>
    request(`/api/projects/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  createSubproject: (id, body) =>
    request(`/api/projects/${id}/subprojects`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  resetPageAnalysis: id =>
    request(`/api/projects/${id}/page-analysis/reset`, {
      method: 'POST',
    }),

  resetSitemapAnalysis: id =>
    request(`/api/projects/${id}/sitemap-analysis/reset`, {
      method: 'POST',
    }),

  // ============================================================
  // PAGE ANALYSIS TARGETS
  // ============================================================

  addTarget: (id, body) =>
    request(`/api/projects/${id}/targets`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  deleteTarget: id =>
    request(`/api/targets/${id}`, {
      method: 'DELETE',
    }),

  // ============================================================
  // KEYWORDS
  // ============================================================

  addKeyword: (id, body) =>
    request(`/api/projects/${id}/keywords`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  importKeywordsCsv: (id, file, mode = 'merge') => {
    const body = new FormData()
    body.append('file', file)

    const qs = new URLSearchParams({ mode })

    return request(
      `/api/projects/${id}/keywords/import-csv?${qs.toString()}`,
      {
        method: 'POST',
        body,
      }
    )
  },

  deleteKeyword: id =>
    request(`/api/keywords/${id}`, {
      method: 'DELETE',
    }),

  // ============================================================
  // PAGE ANALYSIS RUNS
  // ============================================================

  startRun: (id, body) =>
    request(`/api/projects/${id}/runs`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  getRun: id =>
    request(`/api/runs/${id}`),

  cancelRun: id =>
    request(`/api/runs/${id}/cancel`, {
      method: 'POST',
    }),

  phase1: id =>
    request(`/api/runs/${id}/phase1`),

  phase2: id =>
    request(`/api/runs/${id}/phase2`),

  detailedAnalysis: id =>
    request(`/api/runs/${id}/detailed-analysis`),

  opportunities: id =>
    request(`/api/runs/${id}/opportunities`),

  failures: id =>
    request(`/api/runs/${id}/failures`),

  evidence: (runId, evidenceId) =>
    request(
      `/api/runs/${runId}/evidence/${encodeURIComponent(
        evidenceId
      )}`
    ),

  exportUrl: id =>
    `${BASE}/api/runs/${id}/export.xlsx`,

  // ============================================================
  // SITEMAP INTELLIGENCE — SOURCES
  // ============================================================

  listSitemapSources: projectId =>
    request(
      `/api/projects/${projectId}/sitemap-sources`
    ),

  addSitemapSource: (projectId, body) =>
    request(
      `/api/projects/${projectId}/sitemap-sources`,
      {
        method: 'POST',
        body: JSON.stringify(body),
      }
    ),

  deleteSitemapSource: id =>
    request(`/api/sitemap-sources/${id}`, {
      method: 'DELETE',
    }),

  // ============================================================
  // SITEMAP INTELLIGENCE — RUNS
  // ============================================================

  listSitemapRuns: projectId =>
    request(
      `/api/projects/${projectId}/sitemap-runs`
    ),

  startSitemapRun: (projectId, body) =>
    request(
      `/api/projects/${projectId}/sitemap-runs`,
      {
        method: 'POST',
        body: JSON.stringify(body),
      }
    ),

  getSitemapRun: id =>
    request(`/api/sitemap-runs/${id}`),

  cancelSitemapRun: id =>
    request(`/api/sitemap-runs/${id}/cancel`, {
      method: 'POST',
    }),

  recoverSitemapFailures: id =>
    request(`/api/sitemap-runs/${id}/recover-failures`, {
      method: 'POST',
    }),

  // ============================================================
  // SITEMAP RESULT VIEWS
  // ============================================================

  sitemapOverview: id =>
    request(
      `/api/sitemap-runs/${id}/overview`
    ),

  sitemapUrlTypes: id =>
    request(
      `/api/sitemap-runs/${id}/url-types`
    ),

  sitemapTopics: (id, params = {}) => {
    const qs = new URLSearchParams()

    Object.entries(params).forEach(([key, value]) => {
      if (value !== '' && value != null) {
        qs.set(key, String(value))
      }
    })

    return request(
      `/api/sitemap-runs/${id}/topics?${qs.toString()}`
    )
  },

  sitemapHeadToHead: (id, params = {}) => {
    const qs = new URLSearchParams()

    Object.entries(params).forEach(([key, value]) => {
      if (value !== '' && value != null) {
        qs.set(key, String(value))
      }
    })

    return request(
      `/api/sitemap-runs/${id}/head-to-head?${qs.toString()}`
    )
  },

  sitemapMissingCategories: (id, params = {}) => {
    const qs = new URLSearchParams()

    Object.entries(params).forEach(([key, value]) => {
      if (value !== '' && value != null) {
        qs.set(key, String(value))
      }
    })

    return request(
      `/api/sitemap-runs/${id}/missing-categories?${qs.toString()}`
    )
  },

  sitemapOpportunities: (id, params = {}) => {
    const qs = new URLSearchParams()

    Object.entries(params).forEach(([key, value]) => {
      if (value !== '' && value != null) {
        qs.set(key, String(value))
      }
    })

    return request(
      `/api/sitemap-runs/${id}/opportunities?${qs.toString()}`
    )
  },

  sitemapDuplicates: (id, params = {}) => {
    const qs = new URLSearchParams()

    Object.entries(params).forEach(([key, value]) => {
      if (value !== '' && value != null) {
        qs.set(key, String(value))
      }
    })

    return request(
      `/api/sitemap-runs/${id}/duplicates?${qs.toString()}`
    )
  },

  sitemapCanonicals: (id, params = {}) => {
    const qs = new URLSearchParams()

    Object.entries(params).forEach(([key, value]) => {
      if (value !== '' && value != null) {
        qs.set(key, String(value))
      }
    })

    return request(
      `/api/sitemap-runs/${id}/canonicals?${qs.toString()}`
    )
  },

  sitemapFailures: (id, params = {}) => {
    const qs = new URLSearchParams()

    Object.entries(params).forEach(([key, value]) => {
      if (value !== '' && value != null) {
        qs.set(key, String(value))
      }
    })

    return request(
      `/api/sitemap-runs/${id}/failures?${qs.toString()}`
    )
  },

  // ============================================================
  // SITEMAP INVENTORY
  // Supports:
  // q/search
  // type
  // site/source
  // page
  // page_size
  // and any future query params exposed by backend
  // ============================================================

  sitemapInventory: (id, params = {}) => {
    const qs = new URLSearchParams()

    Object.entries(params).forEach(([key, value]) => {
      if (value !== '' && value != null) {
        qs.set(key, String(value))
      }
    })

    return request(
      `/api/sitemap-runs/${id}/inventory?${qs.toString()}`
    )
  },

  // ============================================================
  // SITEMAP EVIDENCE
  // ============================================================

  sitemapEvidence: (runId, urlId) =>
    request(
      `/api/sitemap-runs/${runId}/evidence/${urlId}`
    ),

  // ============================================================
  // SITEMAP EXCEL EXPORT
  // ============================================================

  sitemapExportUrl: id =>
    `${BASE}/api/sitemap-runs/${id}/export.xlsx`,
}

export default api