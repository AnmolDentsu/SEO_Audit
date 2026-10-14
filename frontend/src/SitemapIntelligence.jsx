import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import QueueStatus from './components/QueueStatus.jsx'
import {
  complianceRunFields,
} from './compliance.js'

const TABS = [
  'Overview',
  'URL Types',
  'Topic Coverage',
  'Head-to-Head',
  'Missing Categories',
  'Content Opportunities',
  'Canonical & Duplicates',
  'Inventory',
  'Failures',
]

const LIVE = new Set(['queued', 'claimed', 'running', 'cancelling'])
const EXPORTABLE = new Set(['completed', 'completed_with_warnings', 'partial'])

function displayRunNumber(run) {
  return run?.run_number ?? run?.id ?? '—'
}

const SCOPE_OPTIONS = [
  {
    value: 'registrable_domain',
    label: 'Registrable domain (recommended)',
    help: 'Resolve the root domain and its subdomains. External-domain URLs remain visible in discovery counts but are not fetched.',
  },
  {
    value: 'strict_host',
    label: 'Strict host',
    help: 'Resolve only the exact root hostname after www normalization.',
  },
  {
    value: 'sitemap_authoritative',
    label: 'Sitemap-authoritative',
    help: 'Resolve every page URL declared by the recursive sitemap tree, including external domains. This can greatly increase runtime and cost.',
  },
]

function Badge({ children, tone = '' }) {
  return <span className={`badge ${String(tone).toLowerCase()}`}>{children}</span>
}

function formatDuration(value) {
  const n = Math.max(0, Math.round(Number(value) || 0))
  if (n < 60) return `${n}s`
  const h = Math.floor(n / 3600)
  const m = Math.floor((n % 3600) / 60)
  const s = n % 60
  return h ? `${h}h ${m}m` : s >= 10 ? `${m}m ${s}s` : `${m}m`
}

function formatRate(perSecond) {
  const n = Number(perSecond)
  if (!Number.isFinite(n) || n <= 0) return null
  const perMinute = n * 60
  return perMinute >= 10 ? `${Math.round(perMinute).toLocaleString()} URLs/min` : `${perMinute.toFixed(1)} URLs/min`
}

function Table({ rows }) {
  if (!rows?.length) return <div className="card empty-table">No data yet.</div>
  const headers = Object.keys(rows[0])
  return (
    <div className="card">
      <div className="table-wrap">
        <table>
          <thead>
            <tr>{headers.map(h => <th key={h}>{h}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                {headers.map(h => (
                  <td key={h}>{Array.isArray(r[h]) ? r[h].join(' | ') : (r[h] ?? '—')}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}


function emptyPage(pageSize = 100) {
  return { items: [], total: 0, page: 1, page_size: pageSize }
}

function emptyFailures(pageSize = 100) {
  return {
    items: [],
    total: 0,
    page: 1,
    page_size: pageSize,
    supplemental: [],
    summary: {
      page_resolution_failures: 0,
      supplemental_issues: 0,
      total_failures_and_warnings: 0,
      recoverable: 0,
      stale_404_410: 0,
      rate_limited_429: 0,
      status_counts: {},
    },
  }
}


function emptyDuplicateAudit(pageSize = 100) {
  return {
    items: [],
    total: 0,
    page: 1,
    page_size: pageSize,
    summary: {
      duplicate_records: 0,
      duplicate_groups: 0,
      sitemap_exact_duplicates: 0,
      sitemap_normalized_duplicates: 0,
      cross_source_duplicates: 0,
      canonical_collisions: 0,
      redirect_collisions: 0,
      resolved_identity_collisions: 0,
      historical_duplicate_count: 0,
      evidence_status: 'historical_or_no_duplicates',
    },
  }
}

function emptyCanonicalAudit(pageSize = 100) {
  return {
    items: [],
    total: 0,
    page: 1,
    page_size: pageSize,
    summary: {
      inventory_urls: 0,
      canonical_present: 0,
      canonical_missing: 0,
      canonical_collisions: 0,
      redirect_collisions: 0,
      resolved_identity_collisions: 0,
    },
  }
}

function Pager({ data, load, noun = 'rows' }) {
  const total = Number(data?.total || 0)
  const page = Number(data?.page || 1)
  const pageSize = Number(data?.page_size || 100)
  const lastPage = Math.max(1, Math.ceil(total / Math.max(pageSize, 1)))

  return (
    <div className="pager">
      <span>{total.toLocaleString()} {noun} · page {page} of {lastPage}</span>
      <div>
        <button disabled={page <= 1} onClick={() => load(page - 1)}>Previous</button>
        <button disabled={page >= lastPage} onClick={() => load(page + 1)}>Next</button>
      </div>
    </div>
  )
}

function PagedTable({ data, load, noun = 'rows' }) {
  return (
    <>
      <Table rows={data?.items || []} />
      <Pager data={data || emptyPage()} load={load} noun={noun} />
    </>
  )
}

function Progress({ run }) {
  const [, tick] = useState(0)
  useEffect(() => {
    if (!LIVE.has(run?.status)) return
    const timer = setInterval(() => tick(x => x + 1), 1000)
    return () => clearInterval(timer)
  }, [run?.status])

  const p = run?.config?._progress || {}
  const overall = EXPORTABLE.has(run?.status)
    ? 100
    : Math.max(0, Math.min(100, Number(p.overall_percent ?? p.percent) || 0))
  const derivedStage = p.stage_total > 0
    ? Number(p.stage_current || 0) / Number(p.stage_total) * 100
    : 0
  const stagePercent = EXPORTABLE.has(run?.status)
    ? 100
    : Math.max(0, Math.min(100, Number(p.stage_percent ?? derivedStage) || 0))
  const rate = formatRate(p.stage_rate_per_second)

  return (
    <div className="run-progress-ui">
      <div className="run-progress-heading">
        <div>
          <strong>{p.stage_label || run?.phase || 'Preparing sitemap analysis'}</strong>
          <span>{p.message || 'Working…'}</span>
        </div>
        <div className="progress-percent">Overall {overall.toFixed(overall < 10 ? 1 : 0)}%</div>
      </div>

      <div className={`progress enhanced ${LIVE.has(run?.status) ? 'is-running' : ''}`}>
        <span style={{ width: `${overall}%` }} />
      </div>

      {LIVE.has(run?.status) && p.stage_total > 0 && (
        <>
          <div className="progress-meta" style={{ marginTop: 8 }}>
            <span><b>Current stage</b> {stagePercent.toFixed(stagePercent < 10 ? 2 : 1)}%</span>
          </div>
          <div className="progress enhanced is-running">
            <span style={{ width: `${stagePercent}%` }} />
          </div>
        </>
      )}

      <div className="progress-meta">
        {p.stage_total > 0 && (
          <span>
            <b>Step</b> {Number(p.stage_current || 0).toLocaleString()}/{Number(p.stage_total).toLocaleString()}
          </span>
        )}
        <span>
          <b>{LIVE.has(run?.status) ? 'Elapsed run time' : 'Total run time'}</b> {formatDuration(p.elapsed_seconds)}
        </span>
        {LIVE.has(run?.status) && p.stage_eta_seconds != null && (
          <span><b>Stage ETA</b> {formatDuration(p.stage_eta_seconds)}</span>
        )}
        {rate && LIVE.has(run?.status) && <span><b>Current rate</b> {rate}</span>}
        {LIVE.has(run?.status) && <span className="activity live"><i />Active processing</span>}
      </div>

      {LIVE.has(run?.status) && (
        <small className="eta-note">
          Overall progress is stage-weighted. Current-stage progress and ETA are calculated from the active stage only.
        </small>
      )}
    </div>
  )
}

function liveDiscoveryToOverview(rows = []) {
  return rows.map(x => ({
    Site: x.site,
    Role: x.role,
    'Root Sitemap': x.root_sitemap,
    'Discovery Status': x.status,
    'Scope Mode': x.scope_mode,
    'Raw Sitemap URL Records': x.raw_url_records ?? 0,
    'Unique Sitemap URLs': x.unique_sitemap_urls ?? 0,
    'Sitemap Duplicate Records': x.duplicate_url_records ?? 0,
    'XMLs Traversed': x.xmls_traversed ?? 0,
    'Max XML Depth': x.max_depth ?? 0,
    'Root Host URLs': x.root_host_urls ?? 0,
    'Related Subdomain URLs': x.related_subdomain_urls ?? 0,
    'External Domain URLs': x.external_domain_urls ?? 0,
    'In-Scope URLs': x.in_scope_urls ?? 0,
    'Excluded by Scope': x.excluded_by_scope_urls ?? 0,
    'Excluded External-Domain URLs': x.excluded_external_domain_urls ?? 0,
    'Excluded Non-Page URLs': x.excluded_non_page_urls ?? 0,
    'Resolution Candidates': x.resolution_candidates ?? 0,
    'Resolved / Usable URLs': x.usable_urls ?? 0,
    'Stale 404/410': x.stale_urls ?? 0,
    'Security / WAF Challenges': x.security_challenge_urls ?? 0,
    'Canonical Duplicates': x.canonical_duplicates ?? 0,
    'Other Failed': x.failed_urls ?? 0,
    'Discovery Note': x.message || '',
  }))
}

export default function SitemapIntelligence({
  project,
  publicDataConfirmed = false,
}) {
  const [sources, setSources] = useState([])
  const [runs, setRuns] = useState([])
  const [activeRun, setActiveRun] = useState(null)
  const [tab, setTab] = useState('Overview')

  const [label, setLabel] = useState('')
  const [role, setRole] = useState('COMPETITOR')
  const [url, setUrl] = useState('')
  const [scopeMode, setScopeMode] = useState('registrable_domain')
  const [excludeNonPages, setExcludeNonPages] = useState(true)

  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [evidence, setEvidence] = useState(null)

  const [overview, setOverview] = useState([])
  const [urlTypes, setUrlTypes] = useState([])
  const [topics, setTopics] = useState({
    site_profiles: [],
    taxonomy: emptyPage(100),
    clusters: emptyPage(100),
    coverage: emptyPage(100),
  })
  const [head, setHead] = useState(emptyPage(100))
  const [missing, setMissing] = useState(emptyPage(100))
  const [opps, setOpps] = useState(emptyPage(50))
  const [failures, setFailures] = useState(emptyFailures(100))
  const [inventory, setInventory] = useState({ items: [], total: 0, page: 1, page_size: 100 })
  const [duplicates, setDuplicates] = useState(emptyDuplicateAudit(100))
  const [canonicals, setCanonicals] = useState(emptyCanonicalAudit(100))
  const [query, setQuery] = useState('')
  const [broadType, setBroadType] = useState('')
  const [auditQuery, setAuditQuery] = useState('')
  const [duplicateKind, setDuplicateKind] = useState('')

  const [analysisQuery, setAnalysisQuery] = useState('')
  const [failureQuery, setFailureQuery] = useState('')
  const [failureHost, setFailureHost] = useState('')
  const [failureStatus, setFailureStatus] = useState('')

  const clearResults = () => {
    setOverview([])
    setUrlTypes([])
    setTopics({
      site_profiles: [],
      taxonomy: emptyPage(100),
      clusters: emptyPage(100),
      coverage: emptyPage(100),
    })
    setHead(emptyPage(100))
    setMissing(emptyPage(100))
    setOpps(emptyPage(50))
    setFailures(emptyFailures(100))
    setInventory({ items: [], total: 0, page: 1, page_size: 100 })
    setDuplicates(emptyDuplicateAudit(100))
    setCanonicals(emptyCanonicalAudit(100))
  }

  const loadResults = async (id, filters = {}) => {
    const q = filters.q || undefined
    const [o, u, t, h, m, p, f, inv, dup, can] = await Promise.all([
      api.sitemapOverview(id),
      api.sitemapUrlTypes(id),
      api.sitemapTopics(id, {
        taxonomy_page: 1,
        cluster_page: 1,
        coverage_page: 1,
        page_size: 100,
        q,
      }),
      api.sitemapHeadToHead(id, { page: 1, page_size: 100, q }),
      api.sitemapMissingCategories(id, { page: 1, page_size: 100, q }),
      api.sitemapOpportunities(id, { page: 1, page_size: 50, q }),
      api.sitemapFailures(id, { page: 1, page_size: 100 }),
      api.sitemapInventory(id, { page: 1, page_size: 100 }),
      api.sitemapDuplicates(id, { page: 1, page_size: 100 }),
      api.sitemapCanonicals(id, { page: 1, page_size: 100 }),
    ])
    setOverview(o)
    setUrlTypes(u)
    setTopics(t)
    setHead(h)
    setMissing(m)
    setOpps(p)
    setFailures(f)
    setInventory(inv)
    setDuplicates(dup)
    setCanonicals(can)
  }

  const refreshSetup = async () => {
    const [s, r] = await Promise.all([
      api.listSitemapSources(project.id),
      api.listSitemapRuns(project.id),
    ])
    setSources(s)
    setRuns(r)
    if (!activeRun && r[0]) {
      setActiveRun(r[0])
      if (!LIVE.has(r[0].status)) await loadResults(r[0].id)
    }
  }

  useEffect(() => {
    setActiveRun(null)
    clearResults()
    refreshSetup().catch(e => setMessage(e.message))
  }, [project.id])

  useEffect(() => {
    if (!activeRun?.id || !LIVE.has(activeRun.status)) return
    const timer = setInterval(async () => {
      try {
        const r = await api.getSitemapRun(activeRun.id)
        setActiveRun(r)
        if (!LIVE.has(r.status)) {
          clearInterval(timer)
          await loadResults(r.id)
          await refreshSetup()
        }
      } catch (e) {
        setMessage(e.message)
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [activeRun?.id, activeRun?.status])

  const add = async () => {
    if (!url || !label) return
    await api.addSitemapSource(project.id, { root_url: url, role, label })
    setUrl('')
    setLabel('')
    await refreshSetup()
  }

  const start = async () => {
    setBusy(true)
    setMessage('')
    clearResults()
    try {
      const r = await api.startSitemapRun(project.id, {
        use_cache: true,
        force_refresh: false,
        enable_ai: true,
        resolution_scope: scopeMode,
        exclude_non_page_urls: excludeNonPages,
        ...complianceRunFields(),
      })
      setActiveRun(r)
      setTab('Overview')
    } catch (e) {
      setMessage(e.message)
    } finally {
      setBusy(false)
    }
  }

  const cancelRun = async () => {
    if (!activeRun || !LIVE.has(activeRun.status)) return
    if (!window.confirm(`Stop Sitemap Run #${displayRunNumber(activeRun)}? The worker will terminate the active run and browser process tree. Persisted evidence remains available for audit.`)) return

    setBusy(true)
    try {
      const r = await api.cancelSitemapRun(activeRun.id)
      setActiveRun(r)
      setMessage('Cancellation requested. The worker will stop this sitemap run safely.')
    } catch (e) {
      setMessage(e.message)
    } finally {
      setBusy(false)
    }
  }

  const resetInputs = async () => {
    if (!window.confirm('Reset Sitemap Intelligence inputs for this project? Active sitemap sources will be cleared, historical runs will be preserved, and any live sitemap run will be cancelled.')) return

    setBusy(true)
    try {
      const result = await api.resetSitemapAnalysis(project.id)
      setActiveRun(null)
      clearResults()
      await refreshSetup()
      setMessage(result.message || 'Sitemap inputs reset.')
    } catch (e) {
      setMessage(e.message)
    } finally {
      setBusy(false)
    }
  }

  const selectRun = async r => {
    clearResults()
    setActiveRun(r)
    if (!LIVE.has(r.status)) await loadResults(r.id)
  }

  const loadInventory = async (page = 1) => {
    if (!activeRun) return
    setInventory(await api.sitemapInventory(activeRun.id, {
      page,
      page_size: 100,
      broad_type: broadType,
      q: query,
    }))
  }


  const loadDuplicatePage = async (page = 1) => {
    if (!activeRun) return
    setDuplicates(await api.sitemapDuplicates(activeRun.id, {
      page,
      page_size: 100,
      duplicate_kind: duplicateKind || undefined,
      q: auditQuery || undefined,
    }))
  }

  const loadCanonicalPage = async (page = 1) => {
    if (!activeRun) return
    setCanonicals(await api.sitemapCanonicals(activeRun.id, {
      page,
      page_size: 100,
      q: auditQuery || undefined,
    }))
  }

  const applyAuditFilters = async () => {
    if (!activeRun) return
    const [dup, can] = await Promise.all([
      api.sitemapDuplicates(activeRun.id, {
        page: 1,
        page_size: 100,
        duplicate_kind: duplicateKind || undefined,
        q: auditQuery || undefined,
      }),
      api.sitemapCanonicals(activeRun.id, {
        page: 1,
        page_size: 100,
        q: auditQuery || undefined,
      }),
    ])
    setDuplicates(dup)
    setCanonicals(can)
  }

  const loadTopicPage = async (kind, page) => {
    if (!activeRun) return
    const next = {
      taxonomy_page: Number(topics.taxonomy?.page || 1),
      cluster_page: Number(topics.clusters?.page || 1),
      coverage_page: Number(topics.coverage?.page || 1),
      page_size: 100,
      q: analysisQuery || undefined,
    }
    if (kind === 'taxonomy') next.taxonomy_page = page
    if (kind === 'clusters') next.cluster_page = page
    if (kind === 'coverage') next.coverage_page = page
    setTopics(await api.sitemapTopics(activeRun.id, next))
  }

  const loadHeadPage = async page => {
    if (!activeRun) return
    setHead(await api.sitemapHeadToHead(activeRun.id, {
      page,
      page_size: 100,
      q: analysisQuery || undefined,
    }))
  }

  const loadMissingPage = async page => {
    if (!activeRun) return
    setMissing(await api.sitemapMissingCategories(activeRun.id, {
      page,
      page_size: 100,
      q: analysisQuery || undefined,
    }))
  }

  const loadOpportunityPage = async page => {
    if (!activeRun) return
    setOpps(await api.sitemapOpportunities(activeRun.id, {
      page,
      page_size: 50,
      q: analysisQuery || undefined,
    }))
  }

  const applyAnalysisFilters = async () => {
    if (!activeRun) return
    const q = analysisQuery || undefined
    const [t, h, m, p] = await Promise.all([
      api.sitemapTopics(activeRun.id, {
        taxonomy_page: 1,
        cluster_page: 1,
        coverage_page: 1,
        page_size: 100,
        q,
      }),
      api.sitemapHeadToHead(activeRun.id, { page: 1, page_size: 100, q }),
      api.sitemapMissingCategories(activeRun.id, { page: 1, page_size: 100, q }),
      api.sitemapOpportunities(activeRun.id, { page: 1, page_size: 50, q }),
    ])
    setTopics(t)
    setHead(h)
    setMissing(m)
    setOpps(p)
  }

  const loadFailurePage = async (page = 1) => {
    if (!activeRun) return
    setFailures(await api.sitemapFailures(activeRun.id, {
      page,
      page_size: 100,
      http_status: failureStatus || undefined,
      host: failureHost || undefined,
      q: failureQuery || undefined,
    }))
  }

  const applyFailureFilters = async () => {
    await loadFailurePage(1)
  }

  const recoverFailures = async () => {
    if (!activeRun || LIVE.has(activeRun.status)) return
    const count = Number(failures?.summary?.recoverable || 0)
    if (!count) {
      setMessage('No recoverable persisted failures are available for this run.')
      return
    }
    if (!window.confirm(`Recover ${count.toLocaleString()} eligible URL(s) with host-safe adaptive batching? Successful URLs stay untouched, and a renewed WAF/access block pauses only the affected host.`)) return

    setBusy(true)
    try {
      const r = await api.recoverSitemapFailures(activeRun.id)
      setActiveRun(r)
      setMessage('Adaptive recovery queued. Successful URLs are preserved; hard-access failures are retried in small host-wise batches and stop automatically if blocking returns.')
      await refreshSetup()
    } catch (e) {
      setMessage(e.message)
    } finally {
      setBusy(false)
    }
  }

  const showEvidence = async id => {
    const n = Number(String(id).split(':').pop())
    if (!activeRun || !n) return
    try {
      setEvidence(await api.sitemapEvidence(activeRun.id, n))
    } catch (e) {
      setMessage(e.message)
    }
  }

  const sourceCounts = useMemo(() => ({
    client: sources.filter(x => x.role === 'CLIENT').length,
    competitor: sources.filter(x => x.role === 'COMPETITOR').length,
  }), [sources])

  const liveDiscovery = activeRun?.config?._sitemap_discovery || []
  const displayOverview = overview.length ? overview : liveDiscoveryToOverview(liveDiscovery)
  const liveResolution = activeRun?.config?._sitemap_resolution || {}
  const activeScope = activeRun?.config?.resolution_scope || scopeMode
  const selectedScopeHelp = SCOPE_OPTIONS.find(x => x.value === scopeMode)?.help

  return (
    <div className="sitemap-module">
      {message && <div className="alert" onClick={() => setMessage('')}>{message}</div>}

      <section className="card sitemap-setup">
        <div className="card-title">
          <div>
            <h2>Sitemap Intelligence</h2>
            <p>Discover the full recursive sitemap inventory first, then explicitly choose which discovered URLs are resolved for content analysis.</p>
          </div>
          <div className="sitemap-actions">
            <Badge tone="client">CLIENT {sourceCounts.client}</Badge>
            <Badge>COMPETITORS {sourceCounts.competitor}</Badge>
            <button
              className="danger-outline"
              disabled={busy}
              onClick={resetInputs}
            >
              Reset inputs
            </button>
            <button
              className="primary"
              disabled={
                busy
                || !publicDataConfirmed
                || LIVE.has(activeRun?.status)
                || sourceCounts.client !== 1
                || sourceCounts.competitor < 1
              }
              onClick={start}
            >
              {busy ? 'Starting…' : 'Start sitemap analysis'}
            </button>
          </div>
        </div>

        <div className="sitemap-source-form">
          <input placeholder="Site label" value={label} onChange={e => setLabel(e.target.value)} />
          <select value={role} onChange={e => setRole(e.target.value)}>
            <option>CLIENT</option>
            <option>COMPETITOR</option>
          </select>
          <input placeholder="https://example.com/sitemap.xml" value={url} onChange={e => setUrl(e.target.value)} />
          <button onClick={() => add().catch(e => setMessage(e.message))}>Add sitemap</button>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(260px, 1fr) auto', gap: 12, alignItems: 'center', marginTop: 12 }}>
          <div>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 800, marginBottom: 5 }}>Resolution scope</label>
            <select value={scopeMode} onChange={e => setScopeMode(e.target.value)} style={{ width: '100%' }}>
              {SCOPE_OPTIONS.map(x => <option key={x.value} value={x.value}>{x.label}</option>)}
            </select>
            <small style={{ color: '#738096' }}>{selectedScopeHelp}</small>
          </div>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
            <input type="checkbox" checked={excludeNonPages} onChange={e => setExcludeNonPages(e.target.checked)} />
            Exclude obvious PDFs/assets/documents from page resolution
          </label>
        </div>

        {scopeMode === 'sitemap_authoritative' && (
          <div className="alert" style={{ marginTop: 12 }}>
            <strong>Sitemap-authoritative resolution selected.</strong> Every final page URL declared by the sitemap tree can be fetched, including external domains. Large sitemap indexes can substantially increase runtime and infrastructure cost.
          </div>
        )}

        <div className="mini-list">
          {sources.map(s => (
            <div key={s.id}>
              <Badge tone={s.role}>{s.role}</Badge>
              <strong>{s.label}</strong>
              <span className="truncate">{s.root_url}</span>
              <button className="icon" onClick={() => api.deleteSitemapSource(s.id).then(refreshSetup).catch(e => setMessage(e.message))}>×</button>
            </div>
          ))}
        </div>
      </section>

      {runs.length > 0 && (
        <div className="sitemap-run-strip">
          <span>Recent sitemap runs</span>
          {runs.slice(0, 8).map(r => (
            <button
              key={r.id}
              className={activeRun?.id === r.id ? 'run-chip active' : 'run-chip'}
              onClick={() => selectRun(r)}
            >
              <span>#{displayRunNumber(r)}</span><Badge tone={r.status}>{r.status}</Badge>
            </button>
          ))}
        </div>
      )}

      {activeRun && (
        <>
          <section className="run-status card">
            <div className="run-identity">
              <h2>Sitemap Run #{displayRunNumber(activeRun)}</h2>
              <p>{activeRun.phase || 'queued'} · scope: {activeScope}</p>
            </div>
            <Progress run={activeRun} />
            <Badge tone={activeRun.status}>{activeRun.status}</Badge>
            <div className="run-actions">
              {LIVE.has(activeRun.status) && (
                <button className="danger-outline" disabled={busy} onClick={cancelRun}>
                  {activeRun.status === 'queued' ? 'Cancel run' : 'Stop run'}
                </button>
              )}
              {!LIVE.has(activeRun.status) && Number(failures?.summary?.recoverable || 0) > 0 && (
                <button className="secondary-light recovery-button" disabled={busy} onClick={recoverFailures}>
                  Recover failed URLs ({Number(failures.summary.recoverable).toLocaleString()})
                </button>
              )}
              {EXPORTABLE.has(activeRun.status) && (
                <a className="button-link" href={api.sitemapExportUrl(activeRun.id)}>Export Excel</a>
              )}
            </div>
          </section>

          <QueueStatus
            kind="sitemap"
            run={activeRun}
          />

          {LIVE.has(activeRun.status) && Object.keys(liveResolution).length > 0 && (
            <div className="metric-grid" style={{ marginTop: 12 }}>
              <Metric label="Resolution candidates" value={Number(liveResolution.resolution_candidates || 0).toLocaleString()} />
              <Metric label="Cache hits" value={Number(liveResolution.cache_hits || 0).toLocaleString()} />
              <Metric label="HTTP resolved successfully" value={Number(liveResolution.resolved_successfully || 0).toLocaleString()} />
              <Metric label="HTTP resolution failed" value={Number(liveResolution.resolution_failed || 0).toLocaleString()} />
              <Metric label="Host access deferred" value={Number(liveResolution.host_access_deferred || 0).toLocaleString()} />
              <Metric label="HTTP 429 seen / pending recovery" value={Number(liveResolution.rate_limited_429 || 0).toLocaleString()} />
            </div>
          )}

          {activeRun.status === 'cancelled' && (
            <div className="alert">
              This sitemap run was cancelled. Persisted discovery/resolution evidence remains available; edit the current inputs and start a new project-local run when ready.
            </div>
          )}

          {activeRun.status === 'completed_with_warnings' && (
            <div className="alert">
              <strong>Analysis completed with warnings.</strong> Comparison outputs are available, but some stale, blocked, rate-limited, or discovery-warning rows remain. Use the Failures tab for the exact audit trail and recover eligible rows without a full recrawl.
            </div>
          )}
          {activeRun.status === 'partial' && (
            <div className="alert error">
              <strong>Partial sitemap analysis.</strong> One or more sitemap sources were not fully discovered. Inventory/export are available, but head-to-head gaps and content opportunities should only be used when comparison status says the client and competitors are ready.
            </div>
          )}
          {activeRun.error && <div className="alert error">{activeRun.error}</div>}

          <nav className="tabs detailed-tabs">
            {TABS.map(x => (
              <button key={x} className={tab === x ? 'active' : ''} onClick={() => setTab(x)}>{x}</button>
            ))}
          </nav>

          {['Topic Coverage','Head-to-Head','Missing Categories','Content Opportunities'].includes(tab) && (
            <div className="analysis-filter-bar card">
              <strong>Analysis filters</strong>
              
              <input placeholder="Filter category/topic" value={analysisQuery} onChange={e => setAnalysisQuery(e.target.value)} />
              <button onClick={() => applyAnalysisFilters().catch(e => setMessage(e.message))}>Apply</button>
              
            </div>
          )}

          <section className="result-area">
            {tab === 'Overview' && <Overview rows={displayOverview} opportunities={opps} failures={failures} />}
            {tab === 'URL Types' && <Table rows={urlTypes} />}
            {tab === 'Topic Coverage' && (
              <>
                <div className="logic-note"><strong>Topic Coverage logic:</strong> canonical clusters consolidate repeated or near-equivalent labels. Confidence is taxonomy evidence strength, not an SEO success probability. Variants show the original labels grouped into each canonical topic. Large matrices are served directly from persisted sitemap rows instead of the compact run JSON.</div>
                <Table rows={topics.site_profiles || []} />
                <div className="section-gap" />
                <PagedTable data={topics.taxonomy} load={page => loadTopicPage('taxonomy', page)} noun="taxonomy rows" />
                <div className="section-gap" />
                <PagedTable data={topics.clusters} load={page => loadTopicPage('clusters', page)} noun="clusters" />
                <div className="section-gap" />
                <PagedTable data={topics.coverage} load={page => loadTopicPage('coverage', page)} noun="topics" />
              </>
            )}
            {tab === 'Head-to-Head' && <PagedTable data={head} load={loadHeadPage} noun="comparisons" />}
            {tab === 'Missing Categories' && <PagedTable data={missing} load={loadMissingPage} noun="category gaps" />}
            {tab === 'Content Opportunities' && (
              <>
                <div className="logic-note"><strong>Opportunity logic:</strong> deterministic priority uses normalized competitor support, evidence volume, gap severity and client-taxonomy relevance. AI review can KEEP/REVIEW/NO_CHANGE but cannot alter measured counts.</div>
                <OpportunityList rows={opps.items || []} showEvidence={showEvidence} />
                <Pager data={opps} load={loadOpportunityPage} noun="opportunities" />
              </>
            )}
            {tab === 'Canonical & Duplicates' && (
              <CanonicalDuplicateAudit
                duplicates={duplicates}
                canonicals={canonicals}
                query={auditQuery}
                setQuery={setAuditQuery}
                duplicateKind={duplicateKind}
                setDuplicateKind={setDuplicateKind}
                apply={applyAuditFilters}
                loadDuplicates={loadDuplicatePage}
                loadCanonicals={loadCanonicalPage}
              />
            )}
            {tab === 'Failures' && <FailurePanel data={failures} query={failureQuery} setQuery={setFailureQuery} host={failureHost} setHost={setFailureHost} status={failureStatus} setStatus={setFailureStatus} apply={applyFailureFilters} load={loadFailurePage} recover={recoverFailures} busy={busy} canRecover={!LIVE.has(activeRun.status)} />}
            {tab === 'Inventory' && (
              <Inventory
                data={inventory}
                q={query}
                setQ={setQuery}
                broadType={broadType}
                setBroadType={setBroadType}
                load={loadInventory}
                showEvidence={showEvidence}
              />
            )}
          </section>
        </>
      )}

      {evidence && (
        <div className="drawer">
          <div className="drawer-head"><h3>Sitemap evidence</h3><button onClick={() => setEvidence(null)}>×</button></div>
          <pre>{JSON.stringify(evidence, null, 2)}</pre>
        </div>
      )}
    </div>
  )
}


function CanonicalDuplicateAudit({
  duplicates,
  canonicals,
  query,
  setQuery,
  duplicateKind,
  setDuplicateKind,
  apply,
  loadDuplicates,
  loadCanonicals,
}) {
  const ds = duplicates?.summary || {}
  const cs = canonicals?.summary || {}
  const historicalOnly = ds.evidence_status === 'historical_count_only'
  const historicalUnknown = ds.evidence_status === 'historical_or_no_duplicates'

  return (
    <>
      <div className="logic-note">
        <strong>Audit logic:</strong> sitemap duplicates come directly from repeated or normalization-equivalent XML &lt;loc&gt; records and run-level duplicate discovery. Canonical/redirect collisions are deterministic crawl facts derived from final and canonical URLs; they are not AI classifications.
      </div>

      {historicalOnly && (
        <div className="alert">
          This historical run recorded {Number(ds.historical_duplicate_count || 0).toLocaleString()} duplicate sitemap occurrence(s), but the old pipeline did not persist the exact duplicate URL pairs. Run the XML-only backfill command supplied with this patch to reconstruct the sitemap duplicate list without recrawling page content.
        </div>
      )}

      {historicalUnknown && (
        <div className="logic-note">
          This run predates persistent duplicate-evidence storage. Canonical data remains available from persisted URL rows; exact historical sitemap duplicate pairs are only available after XML-only backfill.
        </div>
      )}

      <div className="metric-grid">
        <Metric label="Duplicate records" value={Number(ds.duplicate_records || 0).toLocaleString()} />
        <Metric label="Duplicate groups" value={Number(ds.duplicate_groups || 0).toLocaleString()} />
        <Metric label="Exact sitemap duplicates" value={Number(ds.sitemap_exact_duplicates || 0).toLocaleString()} />
        <Metric label="Normalized sitemap duplicates" value={Number(ds.sitemap_normalized_duplicates || 0).toLocaleString()} />
        <Metric label="Cross-source duplicates" value={Number(ds.cross_source_duplicates || 0).toLocaleString()} />
        <Metric label="Canonical collisions" value={Number(ds.canonical_collisions || 0).toLocaleString()} />
        <Metric label="Redirect collisions" value={Number(ds.redirect_collisions || 0).toLocaleString()} />
        <Metric label="URLs in inventory" value={Number(cs.inventory_urls || 0).toLocaleString()} />
        <Metric label="Canonical present" value={Number(cs.canonical_present || 0).toLocaleString()} />
        <Metric label="Canonical missing" value={Number(cs.canonical_missing || 0).toLocaleString()} />
      </div>

      <div className="analysis-filter-bar card">
        <strong>Canonical / duplicate filters</strong>
        <input
          placeholder="Search match URL, original URL, final URL, canonical URL or sitemap"
          value={query}
          onChange={e => setQuery(e.target.value)}
        />
        <select value={duplicateKind} onChange={e => setDuplicateKind(e.target.value)}>
          <option value="">ALL DUPLICATE TYPES</option>
          <option value="SITEMAP_EXACT_DUPLICATE">Exact sitemap duplicate</option>
          <option value="SITEMAP_NORMALIZED_DUPLICATE">Normalized sitemap duplicate</option>
          <option value="CROSS_SOURCE_DUPLICATE">Cross-source duplicate</option>
          <option value="CANONICAL_COLLISION">Canonical collision</option>
          <option value="REDIRECT_COLLISION">Redirect collision</option>
          <option value="RESOLVED_IDENTITY_COLLISION">Resolved identity collision</option>
        </select>
        <button onClick={() => apply().catch(() => {})}>Apply</button>
      </div>

      <h3>Duplicate URL audit</h3>
      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {[
                  'Type',
                  'Stage',
                  'Primary Site',
                  'Duplicate Site',
                  'Match URL',
                  'Primary URL',
                  'Duplicate URL',
                  'Primary Sitemap',
                  'Duplicate Sitemap',
                  'Primary Canonical',
                  'Duplicate Canonical',
                ].map(h => <th key={h}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {(duplicates?.items || []).map(x => (
                <tr key={x.id}>
                  <td>{x.duplicate_kind}</td>
                  <td>{x.audit_stage}</td>
                  <td>{x.primary_site}</td>
                  <td>{x.duplicate_site}</td>
                  <td>{x.match_url}</td>
                  <td>{x.primary_url}</td>
                  <td>{x.duplicate_url}</td>
                  <td>{x.primary_child_sitemap || '—'}</td>
                  <td>{x.duplicate_child_sitemap || '—'}</td>
                  <td>{x.primary_canonical_url || '—'}</td>
                  <td>{x.duplicate_canonical_url || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!(duplicates?.items || []).length && <div className="empty-table">No persisted duplicate rows match these filters.</div>}
      </div>
      <Pager data={duplicates || emptyDuplicateAudit()} load={loadDuplicates} noun="duplicate audit rows" />

      <div className="section-gap" />
      <h3>Canonical audit</h3>
      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {[
                  'Site',
                  'Role',
                  'Original URL',
                  'Final URL',
                  'Canonical URL',
                  'Canonical State',
                  'HTTP',
                  'Child Sitemap',
                  'Last Modified',
                ].map(h => <th key={h}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {(canonicals?.items || []).map(x => (
                <tr key={x.id}>
                  <td>{x.site}</td>
                  <td><Badge tone={x.role}>{x.role}</Badge></td>
                  <td>{x.original_url}</td>
                  <td>{x.final_url || '—'}</td>
                  <td>{x.canonical_url || '—'}</td>
                  <td><Badge tone={x.canonical_state}>{x.canonical_state}</Badge></td>
                  <td>{x.http_status ?? '—'}</td>
                  <td>{x.child_sitemap || '—'}</td>
                  <td>{x.lastmod || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!(canonicals?.items || []).length && <div className="empty-table">No canonical audit rows match these filters.</div>}
      </div>
      <Pager data={canonicals || emptyCanonicalAudit()} load={loadCanonicals} noun="canonical audit rows" />
    </>
  )
}


function FailurePanel({ data, query, setQuery, host, setHost, status, setStatus, apply, load, recover, busy, canRecover }) {
  const summary = data?.summary || {}
  const statusCounts = summary.status_counts || {}
  const statuses = Object.keys(statusCounts).filter(x => x !== 'transport/no-status').sort((a, b) => Number(a) - Number(b))

  return (
    <>
      <div className="metric-grid">
        <Metric label="Page-resolution failures" value={Number(summary.page_resolution_failures || 0).toLocaleString()} />
        <Metric label="Recoverable" value={Number(summary.recoverable || 0).toLocaleString()} />
        <Metric label="Host access deferred" value={Number(summary.host_access_deferred || 0).toLocaleString()} />
        <Metric label="HTTP 429 remaining" value={Number(summary.rate_limited_429 || 0).toLocaleString()} />
        <Metric label="Stale 404/410" value={Number(summary.stale_404_410 || 0).toLocaleString()} />
        <Metric label="Discovery / enrichment issues" value={Number(summary.supplemental_issues || 0).toLocaleString()} />
      </div>

      <div className="analysis-filter-bar card failure-filter-bar">
        <strong>Failure filters</strong>
        <input placeholder="Search URL / error" value={query} onChange={e => setQuery(e.target.value)} />
        <input placeholder="Host e.g. razorpay.com" value={host} onChange={e => setHost(e.target.value)} />
        <select value={status} onChange={e => setStatus(e.target.value)}>
          <option value="">ALL HTTP</option>
          {statuses.map(code => <option key={code} value={code}>{code} ({Number(statusCounts[code] || 0).toLocaleString()})</option>)}
        </select>
        <button onClick={() => apply().catch(() => {})}>Apply</button>
        {canRecover && Number(summary.recoverable || 0) > 0 && (
          <button className="secondary-light recovery-button" disabled={busy} onClick={recover}>Recover eligible failures</button>
        )}
      </div>

      <PagedTable data={data || emptyFailures(100)} load={load} noun="page-resolution failures" />

      {(data?.supplemental || []).length > 0 && (
        <>
          <div className="section-gap" />
          <div className="logic-note"><strong>Discovery / enrichment warnings:</strong> these are separate from paginated page-resolution failures and do not automatically make the comparison unusable.</div>
          <Table rows={data.supplemental} />
        </>
      )}
    </>
  )
}


function Overview({ rows, opportunities, failures }) {
  const raw = rows.reduce((a, x) => a + Number(x['Raw Sitemap URL Records'] || 0), 0)
  const unique = rows.reduce((a, x) => a + Number(x['Unique Sitemap URLs'] || x['Discovered URLs'] || 0), 0)
  const sitemapDuplicates = rows.reduce((a, x) => a + Number(x['Sitemap Duplicate Records'] || 0), 0)
  const canonicalDuplicates = rows.reduce((a, x) => a + Number(x['Canonical Duplicates'] || 0), 0)
  const candidates = rows.reduce((a, x) => a + Number(x['Resolution Candidates'] || x['Discovered URLs'] || 0), 0)
  const excludedScope = rows.reduce((a, x) => a + Number(x['Excluded by Scope'] || 0), 0)
  const excludedExternal = rows.reduce((a, x) => a + Number(x['Excluded External-Domain URLs'] || 0), 0)
  const excludedNonPage = rows.reduce((a, x) => a + Number(x['Excluded Non-Page URLs'] || 0), 0)
  const resolved = rows.reduce((a, x) => a + Number(x['Resolved / Usable URLs'] ?? x['Resolved URLs'] ?? 0), 0)
  const product = rows.reduce((a, x) => a + Number(x.Product || 0), 0)
  const content = rows.reduce((a, x) => a + Number(x['Content / Blog'] ?? x.Blog ?? 0), 0)
  const stale = rows.reduce((a, x) => a + Number(x['Stale 404/410'] || 0), 0)
  const successful = rows.filter(x => ['success', 'warning', 'partial'].includes(String(x['Discovery Status'] || '').toLowerCase()) && Number(x['Unique Sitemap URLs'] || x['Discovered URLs'] || 0) > 0).length
  const failed = rows.filter(x => String(x['Discovery Status'] || '').toLowerCase() === 'failed').length

  return (
    <>
      <div className="metric-grid">
        <Metric label="Sources discovered" value={`${successful}/${rows.length}`} />
        <Metric label="Sources failed" value={failed} />
        <Metric label="Raw sitemap URL records" value={raw.toLocaleString()} />
        <Metric label="Unique sitemap URLs" value={unique.toLocaleString()} />
        <Metric label="Sitemap duplicate records" value={sitemapDuplicates.toLocaleString()} />
        <Metric label="Canonical / resolved collisions" value={canonicalDuplicates.toLocaleString()} />
        <Metric label="Resolution candidates" value={candidates.toLocaleString()} />
        <Metric label="Excluded by scope" value={excludedScope.toLocaleString()} />
        <Metric label="Excluded external-domain" value={excludedExternal.toLocaleString()} />
        <Metric label="Excluded non-page" value={excludedNonPage.toLocaleString()} />
        <Metric label="Usable URLs" value={resolved.toLocaleString()} />
        <Metric label="Product pages" value={product.toLocaleString()} />
        <Metric label="Content / Blog pages" value={content.toLocaleString()} />
        <Metric label="Stale sitemap URLs" value={stale.toLocaleString()} />
        <Metric label="Opportunities" value={Number(opportunities?.total ?? opportunities?.length ?? 0).toLocaleString()} />
        <Metric label="Failures / warnings" value={Number(failures?.summary?.total_failures_and_warnings || 0).toLocaleString()} />
      </div>
      <div className="section-gap" />
      <Table rows={rows} />
    </>
  )
}

function Metric({ label, value }) {
  return <div className="metric card"><span>{label}</span><strong>{value}</strong></div>
}

function OpportunityList({ rows, showEvidence }) {
  return (
    <div className="opportunity-list">
      {rows.length ? rows.map(x => (
        <article className="card opportunity" key={x.opportunity_key}>
          <div className="opp-head">
            <Badge tone={x.priority}>{x.priority}</Badge>
            <Badge>{Math.round(Number(x.confidence || 0) * 100)}% evidence strength</Badge>
            <Badge>{x.gap_type}</Badge><Badge>{x.source}</Badge>
          </div>
          <h3>{x.title}</h3>
          <p><strong>{x.category || '—'}{x.topic ? ` · ${x.topic}` : ''}</strong> · Suggested type: {x.recommended_page_type || 'Review'}</p>
          <p>{x.recommendation}</p>
          {x.priority_score != null && <small>Priority score {x.priority_score}/100{x.priority_reason ? ` · ${x.priority_reason}` : ''}</small>}
          <small>Client {x.client_count} · Competitors {x.competitor_count} URLs across {x.competitor_sites} sites · Business relevance {x.business_relevance}</small>
          <div className="evidence-links">
            {(x.evidence_ids || []).map(id => <button className="link" key={id} onClick={() => showEvidence(id)}>{id}</button>)}
          </div>
        </article>
      )) : <div className="card empty-table">No sitemap opportunities detected.</div>}
    </div>
  )
}

function Inventory({ data, q, setQ, broadType, setBroadType, load, showEvidence }) {
  return (
    <div className="card">
      <div className="filters">
        <input placeholder="Search site label, role, original/final/canonical URL, title, category or topic" value={q} onChange={e => setQ(e.target.value)} />
        <select value={broadType} onChange={e => setBroadType(e.target.value)}>
          <option value="">ALL TYPES</option>
          <option>HOMEPAGE</option>
          <option>PRODUCT</option>
          <option>CONTENT</option>
          <option>BLOG</option>
          <option>OTHER</option>
        </select>
        <button onClick={() => load(1)}>Apply</button>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>{['Site', 'Role', 'Original URL', 'Final URL', 'Canonical URL', 'HTTP', 'Broad Type', 'Detailed Type', 'Confidence', 'Category', 'Topic', 'Title', 'Evidence'].map(h => <th key={h}>{h}</th>)}</tr>
          </thead>
          <tbody>
            {(data.items || []).map(x => (
              <tr key={x.id}>
                <td>{x.site}</td>
                <td><Badge tone={x.role}>{x.role}</Badge></td>
                <td>{x.url}</td>
                <td>{x.final_url || '—'}</td>
                <td>{x.canonical_url || '—'}</td>
                <td>{x.http_status ?? '—'}</td>
                <td>{x.broad_page_type}</td>
                <td>{x.detailed_page_type}</td>
                <td>{Math.round(Number(x.type_confidence || 0) * 100)}%</td>
                <td>{x.category}</td>
                <td>{x.topic}</td>
                <td>{x.title || '—'}</td>
                <td><button className="link" onClick={() => showEvidence(x.evidence_id)}>{x.evidence_id}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="pager">
        <span>{Number(data.total || 0).toLocaleString()} URLs · page {data.page}</span>
        <div>
          <button disabled={data.page <= 1} onClick={() => load(data.page - 1)}>Previous</button>
          <button disabled={data.page * data.page_size >= data.total} onClick={() => load(data.page + 1)}>Next</button>
        </div>
      </div>
    </div>
  )
}
