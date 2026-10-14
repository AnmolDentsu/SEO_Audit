import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import SitemapIntelligence from './SitemapIntelligence.jsx'
import AIUsageBanner from './components/AIUsageBanner.jsx'
import QueueStatus from './components/QueueStatus.jsx'
import {
  complianceRunFields,
  getPublicDataConsent,
} from './compliance.js'

const TABS = [
  'Overview',
  'Comparison Matrix',
  'Detailed Results',
  'Page Summary',
  'Content Structure',
  'Section Comparison',
  'Keyword Placement',
  'FAQ Comparison',
  'Content Opportunities',
  'Failures',
]

const LIVE_RUN = new Set(['queued', 'claimed', 'running', 'cancelling'])
const RESULT_READY = new Set(['completed', 'failed', 'dispatch_failed'])

function displayRunNumber(run) {
  return run?.run_number ?? run?.id ?? '—'
}

function flattenProjects(projects = []) {
  const byParent = new Map()
  for (const project of projects) {
    const key = project.parent_project_id == null ? null : Number(project.parent_project_id)
    if (!byParent.has(key)) byParent.set(key, [])
    byParent.get(key).push(project)
  }

  for (const rows of byParent.values()) {
    rows.sort((a, b) => String(a.name).localeCompare(String(b.name)))
  }

  const output = []
  const seen = new Set()

  const visit = (parentId, depth) => {
    for (const project of byParent.get(parentId) || []) {
      if (seen.has(project.id)) continue
      seen.add(project.id)
      output.push({ ...project, depth })
      visit(Number(project.id), depth + 1)
    }
  }

  visit(null, 0)

  // Defensive fallback for legacy/cyclic data: never hide a project.
  for (const project of projects) {
    if (!seen.has(project.id)) output.push({ ...project, depth: 0 })
  }

  return output
}

function Badge({ children, tone = '' }) { return <span className={`badge ${String(tone).toLowerCase()}`}>{children}</span> }

function ReadinessChip({ label, ok }) {
  return <Badge tone={ok ? 'completed' : 'failed'}>{label} {ok ? '✓' : '✕'}</Badge>
}

function BenchmarkReadiness({ project, phase1 = [], phase2 = [], runtime = false }) {
  if (!runtime) {
    const cfg = project?.benchmark_configuration || {}
    return <div className="filters">
      <strong>Benchmark readiness</strong>
      <ReadinessChip label="CLIENT" ok={Boolean(cfg.client_configured)} />
      <ReadinessChip label="P0" ok={Boolean(cfg.p0_configured)} />
      <ReadinessChip label="P1" ok={Boolean(cfg.p1_configured)} />
      {Number(cfg.other_count || 0) > 0 && <Badge>OTHER {Number(cfg.other_count)}</Badge>}
      <Badge>{cfg.state || 'NO_ANCHORS'}</Badge>
    </div>
  }

  const successful = role => phase2.some(row => {
    const status = Number(row.status_code)
    return row.role === role && !row.error && (!Number.isFinite(status) || (status >= 200 && status < 400))
  })
  const anyReady = phase1.some(row => row.role === 'CLIENT' && row.benchmark_ready)

  return <div className="filters">
    <strong>Run benchmark readiness</strong>
    <ReadinessChip label="CLIENT" ok={successful('CLIENT')} />
    <ReadinessChip label="P0" ok={successful('P0')} />
    <ReadinessChip label="P1" ok={successful('P1')} />
    {anyReady ? <Badge tone="completed">RANGE READY</Badge> : <Badge>RANGE UNAVAILABLE</Badge>}
  </div>
}

function fmt(n, digits = 3) { return Number.isFinite(Number(n)) ? Number(n).toFixed(digits) : '—' }
function sv(n) { return n == null ? '—' : Number(n).toLocaleString() }

function formatDuration(value) {
  const total = Math.max(0, Math.round(Number(value) || 0))
  if (total < 60) return `${total}s`
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours) return `${hours}h ${minutes}m`
  return seconds >= 10 ? `${minutes}m ${seconds}s` : `${minutes}m`
}

function parseBackendTimestamp(value) {
  if (!value) return null
  const raw = String(value).trim()
  // SQLite commonly returns naive UTC timestamps (no Z/offset). Treat those
  // as UTC rather than browser-local time; otherwise IST adds a false 5h30m.
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw)
  const normalized = hasTimezone ? raw : `${raw}Z`
  const ms = new Date(normalized).getTime()
  return Number.isFinite(ms) ? ms : null
}

function phaseFallbackLabel(phase) {
  const labels = {
    initializing: 'Preparing analysis',
    crawl: 'Crawling and extracting pages',
    phase1_density: 'Calculating keyword density',
    phase2_opportunities: 'Building deterministic opportunities',
    phase2_detailed_analysis: 'Comparing sections, keywords and FAQs',
    ai_deep_analysis: 'AI model deep section analysis',
    finalizing: 'Finalizing report data',
    complete: 'Analysis complete',
    failed: 'Analysis failed',
    cancelling: 'Stopping analysis',
    cancelled: 'Analysis cancelled',
  }
  return labels[phase] || String(phase || 'Queued').replaceAll('_', ' ')
}

function RunProgress({ run }) {
  const [, setTick] = useState(0)
  const isLive = LIVE_RUN.has(run.status)

  useEffect(() => {
    if (!isLive) return undefined
    const timer = setInterval(() => setTick(x => x + 1), 1000)
    return () => clearInterval(timer)
  }, [isLive])

  const meta = run.config?._progress || {}
  const fallbackPercent = run.progress_total
    ? (Number(run.progress_current || 0) / Number(run.progress_total)) * 100
    : 0
  const percent = Math.max(0, Math.min(100, Number(meta.percent ?? fallbackPercent) || 0))
  const startedMs = parseBackendTimestamp(run.started_at)
  const heartbeatMs = parseBackendTimestamp(meta.heartbeat_at)
  const heartbeatAge = heartbeatMs ? Math.max(0, Math.floor((Date.now() - heartbeatMs) / 1000)) : null
  const persistedElapsed = Number(meta.elapsed_seconds)
  let elapsedSeconds
  if (Number.isFinite(persistedElapsed) && persistedElapsed >= 0) {
    // Backend elapsed time is authoritative. While live, add only the seconds
    // since the last heartbeat so the counter still ticks smoothly.
    elapsedSeconds = Math.floor(
      persistedElapsed + (isLive && heartbeatAge != null ? heartbeatAge : 0)
    )
  } else if (startedMs) {
    elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedMs) / 1000))
  } else {
    elapsedSeconds = 0
  }
  const stageLabel = meta.stage_label || phaseFallbackLabel(run.phase)
  const message = meta.message || stageLabel
  const stageProgress = meta.stage_total != null && Number(meta.stage_total) > 0
    ? `${Number(meta.stage_current || 0).toLocaleString()}/${Number(meta.stage_total).toLocaleString()}`
    : null

  let activityText = ''
  let activityTone = 'live'
  if (isLive && heartbeatAge != null) {
    if (heartbeatAge <= 6) activityText = 'Active now'
    else if (heartbeatAge <= 20) activityText = `Working · last update ${heartbeatAge}s ago`
    else {
      activityText = `Current operation is still running · last update ${heartbeatAge}s ago`
      activityTone = 'waiting'
    }
  }

  return <div className="run-progress-ui">
    <div className="run-progress-heading">
      <div>
        <strong>{stageLabel}</strong>
        <span>{message}</span>
      </div>
      <div className="progress-percent">{run.status === 'completed' ? '100%' : `${percent.toFixed(percent < 10 ? 1 : 0)}%`}</div>
    </div>

    <div className={`progress enhanced ${isLive ? 'is-running' : ''}`}>
      <span style={{ width: `${run.status === 'completed' ? 100 : percent}%` }} />
    </div>

    <div className="progress-meta">
      {stageProgress && <span><b>Step</b> {stageProgress}</span>}
      <span><b>{isLive ? 'Elapsed run time' : 'Total run time'}</b> {formatDuration(elapsedSeconds)}</span>
      {isLive && meta.eta_seconds != null && <span><b>Approx. ETA</b> {formatDuration(meta.eta_seconds)}</span>}
      {activityText && <span className={`activity ${activityTone}`}><i />{activityText}</span>}
    </div>

    {isLive && <small className="eta-note">ETA is an estimate and can change for slow websites, retries, semantic embeddings, or AI model reasoning.</small>}
  </div>
}

export default function App() {
  const [projects, setProjects] = useState([])
  const [project, setProject] = useState(null)
  const [selectedProject, setSelectedProject] = useState('')
  const [activeRun, setActiveRun] = useState(null)
  const [tab, setTab] = useState('Overview')
  const [phase1, setPhase1] = useState([])
  const [phase2, setPhase2] = useState([])
  const [detail, setDetail] = useState({
    content_structure: [],
    section_comparison: [],
    keyword_placement: [],
    faq_comparison: [],
    content_opportunities: [],
  })
  const [opps, setOpps] = useState([])
  const [failures, setFailures] = useState([])
  const [evidence, setEvidence] = useState(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [analysisMode, setAnalysisMode] = useState('page')
  const [publicDataConfirmed, setPublicDataConfirmed] = useState(
    getPublicDataConsent()
  )

  const clearPageResults = () => {
    setActiveRun(null)
    setPhase1([])
    setPhase2([])
    setDetail({
      content_structure: [],
      section_comparison: [],
      keyword_placement: [],
      faq_comparison: [],
      content_opportunities: [],
    })
    setOpps([])
    setFailures([])
    setEvidence(null)
    setTab('Overview')
  }

  const loadResults = async (runId) => {
    const [p1, p2, granular, o, f] = await Promise.all([
      api.phase1(runId),
      api.phase2(runId),
      api.detailedAnalysis(runId),
      api.opportunities(runId),
      api.failures(runId),
    ])
    setPhase1(p1)
    setPhase2(p2)
    setDetail(granular || {})
    setOpps(o)
    setFailures(f)
  }

  const refreshProjects = async () => {
    const data = await api.listProjects()
    setProjects(data)

    if (!selectedProject && data[0]) {
      setSelectedProject(String(data[0].id))
    }

    return data
  }

  const loadProject = async (id, { selectLatest = true } = {}) => {
    if (!id) {
      setProject(null)
      return null
    }

    const data = await api.getProject(id)
    setProject(data)

    if (selectLatest && !activeRun && data.runs?.[0]) {
      const latest = data.runs[0]
      setActiveRun(latest)
      if (RESULT_READY.has(latest.status)) {
        await loadResults(latest.id)
      }
    }

    return data
  }

  const switchProject = value => {
    setSelectedProject(value)
    setProject(null)
    clearPageResults()
    setMessage('')
  }

  useEffect(() => {
    const handler = event => {
      setPublicDataConfirmed(
        Boolean(event.detail?.confirmed)
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

  useEffect(() => {
    refreshProjects().catch(e => setMessage(e.message))
  }, [])

  useEffect(() => {
    if (!selectedProject) {
      setProject(null)
      return
    }

    loadProject(selectedProject).catch(e => setMessage(e.message))
  }, [selectedProject])

  useEffect(() => {
    if (!activeRun?.id || !LIVE_RUN.has(activeRun.status)) return

    const timer = setInterval(async () => {
      try {
        const run = await api.getRun(activeRun.id)
        setActiveRun(run)

        if (!LIVE_RUN.has(run.status)) {
          clearInterval(timer)
          if (RESULT_READY.has(run.status)) {
            await loadResults(run.id)
          }
          await loadProject(run.project_id, { selectLatest: false })
        }
      } catch (e) {
        setMessage(e.message)
      }
    }, 2000)

    return () => clearInterval(timer)
  }, [activeRun?.id, activeRun?.status])

  const selectRun = async run => {
    setActiveRun(run)
    setEvidence(null)
    setPhase1([])
    setPhase2([])
    setDetail({
      content_structure: [],
      section_comparison: [],
      keyword_placement: [],
      faq_comparison: [],
      content_opportunities: [],
    })
    setOpps([])
    setFailures([])

    if (RESULT_READY.has(run.status)) {
      await loadResults(run.id)
    }
  }

  const startRun = async () => {
    if (!project) return

    setBusy(true)
    setMessage('')
    clearPageResults()

    try {
      const run = await api.startRun(project.id, {
        use_cache: true,
        force_refresh: false,
        enable_ai: true,
        ...complianceRunFields(),
      })
      setActiveRun(run)
      setTab('Overview')
      await loadProject(project.id, { selectLatest: false })
    } catch (e) {
      setMessage(e.message)
    } finally {
      setBusy(false)
    }
  }

  const cancelRun = async () => {
    if (!activeRun || !LIVE_RUN.has(activeRun.status)) return
    if (!window.confirm(`Stop Page Analysis Run #${displayRunNumber(activeRun)}? Partial work is kept for audit, but the run will not continue.`)) return

    setBusy(true)
    try {
      const run = await api.cancelRun(activeRun.id)
      setActiveRun(run)
      setMessage('Cancellation requested. The worker will stop the run and its browser child processes.')
    } catch (e) {
      setMessage(e.message)
    } finally {
      setBusy(false)
    }
  }

  const resetPageAnalysis = async () => {
    if (!project) return
    if (!window.confirm('Reset Page Analysis inputs for this project? Active URLs and keywords will be cleared, historical runs will be preserved, and any live Page Analysis run will be cancelled.')) return

    setBusy(true)
    try {
      const result = await api.resetPageAnalysis(project.id)
      clearPageResults()
      await loadProject(project.id, { selectLatest: false })
      setMessage(result.message || 'Page Analysis inputs reset.')
    } catch (e) {
      setMessage(e.message)
    } finally {
      setBusy(false)
    }
  }

  const showEvidence = async id => {
    if (!activeRun || !id) return
    try {
      setEvidence(await api.evidence(activeRun.id, id))
    } catch (e) {
      setMessage(e.message)
    }
  }

  const flatProjects = useMemo(() => flattenProjects(projects), [projects])

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">SEO</div><div><strong>Analysis Platform</strong><small>Deterministic + AI model</small></div></div>

      <label>Project</label>
      <select value={selectedProject} onChange={e => switchProject(e.target.value)}>
        <option value="">Select project</option>
        {flatProjects.map(p => (
          <option value={p.id} key={p.id}>
            {`${'— '.repeat(p.depth)}${p.name}`}
          </option>
        ))}
      </select>

      <button className="secondary" onClick={() => document.getElementById('new-project').showModal()}>+ New project</button>
      {project && <button className="secondary" onClick={() => document.getElementById('manage-project').showModal()}>Manage project</button>}

      {project && <>
        <div className="analysis-mode-switch">
          <button className={analysisMode === 'page' ? 'active' : ''} onClick={() => setAnalysisMode('page')}>Page Analysis</button>
          <button className={analysisMode === 'sitemap' ? 'active' : ''} onClick={() => setAnalysisMode('sitemap')}>Sitemap Intelligence</button>
        </div>

        {analysisMode === 'page' && <>
          <div className="side-section"><span>Targets</span><strong>{project.targets.length}</strong></div>
          <div className="side-section"><span>Keywords</span><strong>{project.keywords.length}</strong></div>
          <div className="runs-title">Recent runs</div>
          <div className="run-list">
            {project.runs.map(r => (
              <button
                key={r.id}
                onClick={() => selectRun(r)}
                className={activeRun?.id === r.id ? 'run active' : 'run'}
              >
                <span>Run #{displayRunNumber(r)}</span>
                <Badge tone={r.status}>{r.status}</Badge>
              </button>
            ))}
          </div>
        </>}
      </>}
    </aside>

    <main>
      <header className="topbar">
        <div>
          <h1>{project?.name || 'SEO AI Platform'}</h1>
          <p>{project?.client_name || 'Create or select a project to begin.'}</p>
        </div>

        {project && analysisMode === 'page' && (
          <div className="topbar-actions">
            <button className="danger-outline" disabled={busy} onClick={resetPageAnalysis}>
              Reset inputs
            </button>
            <button
              className="primary"
              disabled={
                busy
                || !publicDataConfirmed
                || LIVE_RUN.has(activeRun?.status)
                || !project.targets.length
                || !project.keywords.length
              }
              onClick={startRun}
            >
              {busy ? 'Working…' : 'Start analysis'}
            </button>
          </div>
        )}
      </header>

      {message && <div className="alert" onClick={() => setMessage('')}>{message}</div>}

      <AIUsageBanner />

      {project ? (
        analysisMode === 'sitemap'
          ? (
            <SitemapIntelligence
              key={project.id}
              project={project}
              publicDataConfirmed={publicDataConfirmed}
            />
          )
          : <>
            <section className="config-grid">
              <TargetPanel key={`targets-${project.id}`} project={project} onChange={() => loadProject(project.id, { selectLatest: false })} />
              <KeywordPanel key={`keywords-${project.id}`} project={project} onChange={() => loadProject(project.id, { selectLatest: false })} />
            </section>

            {activeRun && <>
              <section className="run-status card">
                <div className="run-identity">
                  <h2>Run #{displayRunNumber(activeRun)}</h2>
                  <p>{phaseFallbackLabel(activeRun.phase)}</p>
                </div>

                <RunProgress run={activeRun} />
                <Badge tone={activeRun.status}>{activeRun.status}</Badge>

                <div className="run-actions">
                  {LIVE_RUN.has(activeRun.status) && (
                    <button className="danger-outline" disabled={busy} onClick={cancelRun}>
                      {activeRun.status === 'queued' ? 'Cancel run' : 'Stop run'}
                    </button>
                  )}
                  {activeRun.status === 'completed' && (
                    <a className="button-link" href={api.exportUrl(activeRun.id)}>Export Excel</a>
                  )}
                </div>

                {activeRun.status === 'completed' && (
                  <div className="run-readiness">
                    <BenchmarkReadiness project={project} phase1={phase1} phase2={phase2} runtime />
                  </div>
                )}
              </section>

              <QueueStatus
                kind="page"
                run={activeRun}
              />

              {activeRun.error && <div className="alert error">{activeRun.error}</div>}
              {activeRun.status === 'cancelled' && (
                <div className="alert">This run was cancelled. Its historical audit row is preserved; edit the current inputs and start a new run when ready.</div>
              )}

              <nav className="tabs detailed-tabs">
                {TABS.map(t => <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>{t}</button>)}
              </nav>

              <section className="result-area">
                {tab === 'Overview' && <Overview run={activeRun} phase1={phase1} phase2={phase2} detail={detail} failures={failures} />}
                {tab === 'Comparison Matrix' && <ComparisonMatrix rows={phase1} />}
                {tab === 'Detailed Results' && <DetailedResults rows={phase1} showEvidence={showEvidence} />}
                {tab === 'Page Summary' && <PageSummary rows={phase2} showEvidence={showEvidence} />}
                {tab === 'Content Structure' && <DictTable rows={detail.content_structure || []} showEvidence={showEvidence} />}
                {tab === 'Section Comparison' && <><div className="logic-note"><strong>Section logic:</strong> 75% semantic + 25% lexical topic match; coverage also checks client-vs-benchmark depth. Priority is deterministic and evidence-backed; click evidence IDs to audit the source sections.</div><SectionComparison rows={detail.section_comparison || []} showEvidence={showEvidence} /></>}
                {tab === 'Keyword Placement' && <DictTable rows={detail.keyword_placement || []} />}
                {tab === 'FAQ Comparison' && <><div className="logic-note"><strong>FAQ logic:</strong> Composite match = 65% semantic + 25% lexical specificity + 10% question-intent agreement. A high embedding score alone cannot mark a FAQ covered.</div><DictTable rows={detail.faq_comparison || []} /></>}
                {tab === 'Content Opportunities' && <><div className="logic-note"><strong>Opportunity logic:</strong> High / Medium / Low are editorial priorities, never P0/P1 benchmark roles. Confidence labels are evidence strength and each recommendation carries a rule/evidence trail.</div><ContentOpportunities rows={detail.content_opportunities || []} showEvidence={showEvidence} /></>}
                {tab === 'Failures' && <Failures rows={failures} />}
              </section>
            </>}
          </>
      ) : <EmptyState />}
    </main>

    {evidence && <div className="drawer"><div className="drawer-head"><h3>Evidence {evidence.evidence_id}</h3><button onClick={() => setEvidence(null)}>×</button></div><pre>{JSON.stringify(evidence, null, 2)}</pre></div>}

    <NewProjectDialog
      projects={projects}
      onCreated={async p => {
        await refreshProjects()
        switchProject(String(p.id))
      }}
    />

    {project && (
      <ProjectManagerDialog
        key={`manage-${project.id}`}
        project={project}
        projects={projects}
        onChanged={async updated => {
          await refreshProjects()
          setSelectedProject(String(updated.id))
          clearPageResults()
          await loadProject(updated.id, { selectLatest: true })
        }}
        onSubprojectCreated={async child => {
          await refreshProjects()
          switchProject(String(child.id))
        }}
      />
    )}
  </div>
}

function TargetPanel({ project, onChange }) {
  const [url, setUrl] = useState(''); const [role, setRole] = useState('OTHER'); const [label, setLabel] = useState('')
  const add = async () => { if (!url) return; await api.addTarget(project.id, {url, role, label: label || null}); setUrl(''); setLabel(''); onChange() }
  return <div className="card"><div className="card-title"><div><h2>URLs</h2><p>CLIENT is evaluated; P0 and P1 are benchmark anchors. For each keyword, the lower anchor density becomes Target Min and the higher anchor density becomes Target Max; OTHER expands the benchmark pool.</p></div><Badge>{project.targets.length}</Badge></div>
    <BenchmarkReadiness project={project} />
    <div className="inline-form"><input placeholder="https://…" value={url} onChange={e => setUrl(e.target.value)} /><select value={role} onChange={e => setRole(e.target.value)}><option>CLIENT</option><option>P0</option><option>P1</option><option>OTHER</option></select><input placeholder="Label" value={label} onChange={e => setLabel(e.target.value)} /><button onClick={() => add().catch(alert)}>Add</button></div>
    <div className="mini-list">{project.targets.map(t => <div key={t.id}><Badge tone={t.role}>{t.role}</Badge><span className="truncate">{t.label ? `${t.label} — ` : ''}{t.url}</span><button className="icon" onClick={() => api.deleteTarget(t.id).then(onChange)}>×</button></div>)}</div>
  </div>
}

function KeywordPanel({ project, onChange }) {
  const [keyword, setKeyword] = useState('')
  const [keywordType, setKeywordType] = useState('PRIMARY')
  const [searchVolume, setSearchVolume] = useState('')
  const [importing, setImporting] = useState(false)
  const [importMode, setImportMode] = useState('merge')
  const [importMessage, setImportMessage] = useState('')

  const add = async () => {
    if (!keyword.trim()) return

    await api.addKeyword(project.id, {
      keyword: keyword.trim(),
      keyword_type: keywordType,
      search_volume: searchVolume === ''
        ? null
        : Number(String(searchVolume).replace(/,/g, '')),
    })

    setKeyword('')
    setSearchVolume('')
    await onChange()
  }

  const importCsv = async file => {
    setImporting(true)
    setImportMessage('')

    try {
      const result = await api.importKeywordsCsv(project.id, file, importMode)
      const bits = [
        `${String(result.mode || importMode).replace('_', ' ').toUpperCase()}`,
        `new ${result.imported || 0}`,
        `updated ${result.updated || 0}`,
      ]

      if (result.reactivated) bits.push(`reactivated ${result.reactivated}`)
      if (result.deactivated) bits.push(`removed from active set ${result.deactivated}`)
      if (result.unchanged) bits.push(`unchanged ${result.unchanged}`)
      if (result.skipped_file_duplicates) bits.push(`file duplicates ${result.skipped_file_duplicates}`)
      if (result.skipped_blank) bits.push(`blank rows ${result.skipped_blank}`)
      if (result.error_count) bits.push(`row errors ${result.error_count}`)
      bits.push(`active keywords ${result.active_keyword_count ?? project.keywords.length}`)

      setImportMessage(`CSV import complete · ${bits.join(' · ')}`)
      await onChange()
    } catch (e) {
      setImportMessage(`ERROR: ${e.message}`)
    } finally {
      setImporting(false)
    }
  }

  return <div className="card">
    <div className="card-title">
      <div>
        <h2>Keywords</h2>
        <p>CSV accepts Keyword, Keyword Type and optional Search Volume. Choose how this upload changes the current project's active keyword set.</p>
      </div>
      <div className="csv-import-controls">
        <select value={importMode} onChange={e => setImportMode(e.target.value)} disabled={importing}>
          <option value="merge">Merge / update</option>
          <option value="replace">Replace active set</option>
          <option value="add_only">Add new only</option>
        </select>
        <label className="upload">
          {importing ? 'Importing…' : 'Import CSV'}
          <input
            type="file"
            accept=".csv,text/csv"
            disabled={importing}
            onChange={e => {
              const file = e.currentTarget.files?.[0]
              // Reset the browser file input immediately so selecting the same
              // file again still triggers onChange.
              e.currentTarget.value = ''
              if (file) importCsv(file)
            }}
          />
        </label>
      </div>
    </div>

    <div className="logic-note">
      <strong>Merge:</strong> add new + update supplied metadata.{' '}
      <strong>Replace:</strong> this CSV becomes the active keyword set while old run history is preserved.{' '}
      <strong>Add new only:</strong> do not modify existing active keyword metadata.
    </div>

    {importMessage && (
      <div className={importMessage.startsWith('ERROR:') ? 'alert error' : 'import-message'}>
        {importMessage}
      </div>
    )}

    <div className="keyword-form">
      <input placeholder="Keyword / phrase" value={keyword} onChange={e => setKeyword(e.target.value)} />
      <select value={keywordType} onChange={e => setKeywordType(e.target.value)}>
        <option>PRIMARY</option>
        <option>SECONDARY</option>
        <option>OTHER</option>
      </select>
      <input inputMode="numeric" placeholder="Search volume" value={searchVolume} onChange={e => setSearchVolume(e.target.value)} />
      <button onClick={() => add().catch(e => setImportMessage(`ERROR: ${e.message}`))}>Add</button>
    </div>

    <div className="mini-list">
      {project.keywords.map(k => (
        <div key={k.id}>
          <Badge tone={k.keyword_type || 'other'}>{k.keyword_type || 'OTHER'}</Badge>
          <span>{k.keyword}</span>
          <small>{k.search_volume == null ? 'SV —' : `SV ${Number(k.search_volume).toLocaleString()}`}</small>
          <button className="icon" onClick={() => api.deleteKeyword(k.id).then(onChange)}>×</button>
        </div>
      ))}
    </div>
  </div>
}


function Overview({ run, phase1, phase2, detail, failures }) {
  const clientRows = phase1.filter(x => x.role === 'CLIENT')
  const absent = clientRows.filter(x => !x.present).length
  return <div className="metric-grid">
    <Metric label="Run status" value={run.status} />
    <Metric label="Density cells" value={phase1.length} />
    <Metric label="Client absent phrases" value={absent} />
    <Metric label="Successful pages" value={phase2.filter(x => !x.error).length} />
    <Metric label="Sections extracted" value={(detail.content_structure || []).length} />
    <Metric label="Section topics compared" value={(detail.section_comparison || []).length} />
    <Metric label="FAQ topics compared" value={(detail.faq_comparison || []).length} />
    <Metric label="Content opportunities" value={(detail.content_opportunities || []).length} />
    <Metric label="Failures" value={failures.length} />
  </div>
}
function Metric({label,value}) { return <div className="metric card"><span>{label}</span><strong>{value}</strong></div> }

function ComparisonMatrix({ rows }) {
  const matrix = useMemo(() => {
    const byKw = new Map()
    rows.forEach(r => {
      if (!byKw.has(r.keyword)) byKw.set(r.keyword, {keyword:r.keyword, type:r.keyword_type, sv:r.search_volume, client:null, p0:null, p1:null, others:[]})
      const x = byKw.get(r.keyword)
      if (r.role === 'CLIENT') x.client = r
      else if (r.role === 'P0') x.p0 = r
      else if (r.role === 'P1') x.p1 = r
      else x.others.push(r)
    })
    return [...byKw.values()].map(x => {
      const benchmark = x.client || x.p0 || x.p1 || x.others[0] || {}
      const median = benchmark.competitor_pool_median
      const clientDensity = x.client?.density_pct
      return [
        x.keyword, x.type, sv(x.sv), fmt(clientDensity), benchmark.target_min == null ? '—' : fmt(benchmark.target_min), benchmark.target_max == null ? '—' : fmt(benchmark.target_max),
        benchmark.benchmark_state || '—', x.client?.target_status || '—',
        median == null ? '—' : fmt(median),
        clientDensity != null && median != null ? fmt(Number(clientDensity) - Number(median)) : '—',
      ]
    })
  }, [rows])
  return <div className="card"><Table headers={['Keyword','Type','Search Volume','Client Density %','Target Min %','Target Max %','Benchmark State','Client Status','Pool Median %','Client vs Median Gap %']} rows={matrix} /></div>
}

function DetailedResults({ rows, showEvidence }) {
  const [q,setQ] = useState(''); const [role,setRole] = useState('ALL')
  const filtered = useMemo(() => rows.filter(r => (role==='ALL'||r.role===role) && `${r.keyword} ${r.url}`.toLowerCase().includes(q.toLowerCase())), [rows,q,role])
  return <div className="card"><div className="filters"><input placeholder="Filter keyword or URL" value={q} onChange={e=>setQ(e.target.value)} /><select value={role} onChange={e=>setRole(e.target.value)}><option>ALL</option><option>CLIENT</option><option>P0</option><option>P1</option><option>OTHER</option></select></div><Table headers={['Role','URL','Keyword','Type','Search Volume','Count','Words','Density %','Target Min','Target Max','Status','Present','Evidence']} rows={filtered.map(r => [<Badge tone={r.role}>{r.role}</Badge>, r.url, r.keyword, r.keyword_type, sv(r.search_volume), r.count, r.word_count ?? '—', fmt(r.density_pct), r.target_min == null ? '—' : fmt(r.target_min), r.target_max == null ? '—' : fmt(r.target_max), r.target_status || '—', r.present ? 'Yes':'No', <button className="link" onClick={()=>showEvidence(r.evidence_id)}>{r.evidence_id}</button>])}/></div>
}

function PageSummary({ rows, showEvidence }) { return <div className="card"><Table headers={['Role','Label','URL','Final URL','HTTP','Words','Title','H1','H2 count','Retry','Cache','Evidence']} rows={rows.map(r => [<Badge tone={r.role}>{r.role}</Badge>, r.label || '—', r.url, r.final_url || '—', r.status_code ?? '—', r.word_count, r.title || '—', (r.h1||[]).join(' | ') || '—', (r.h2||[]).length, r.retry_count, r.from_cache?'Yes':'No', <button className="link" onClick={()=>showEvidence(r.evidence_id)}>{r.evidence_id}</button>])}/></div> }

function SectionComparison({ rows, showEvidence }) {
  return <div className="card">
    <div className="logic-note"><strong>Decision logic:</strong> Composite Topic Match = 75% semantic similarity + 25% lexical corroboration. Coverage also checks benchmark support and client depth. Priority Score combines missing/weak coverage, P0+P1 support and matched keyword importance. Evidence Confidence is evidence strength, not predicted SEO uplift.</div>
    <Table headers={['Topic','Evidence','P0 Heading','P0 Words','P1 Heading','P1 Words','Client Heading','Client Words','Semantic','Lexical','Composite Match','Depth Ratio','Coverage','Priority Score','Priority','Evidence Confidence','Reason','Benchmark Range','Matched Keywords','Evidence IDs']} rows={rows.map(r => [ r['Benchmark Topic'], r['Benchmark Evidence'], r['P0 Heading']||'—', r['P0 Section Words']||'—', r['P1 Heading']||'—', r['P1 Section Words']||'—', r['Client Heading']||'—', r['Client Section Words'],
      r['Semantic Similarity'] ?? '—', r['Lexical Similarity'] ?? '—', r['Composite Topic Match'] ?? '—', r['Depth Ratio vs Benchmark'] ?? '—', r['Coverage Status'], r['Priority Score'] ?? '—',
      <Badge tone={r['Opportunity Priority']}>{r['Opportunity Priority']}</Badge>, r['Evidence Confidence'] ?? '—', r['Priority Reason'] || '—', r['Benchmark Word Range']||'—', r['Matched Input Keywords']||'—',
      <div className="evidence-links">{['Client Evidence ID','P0 Evidence ID','P1 Evidence ID'].map(k => r[k] ? <button className="link" key={k} onClick={()=>showEvidence(r[k])}>{r[k]}</button> : null)}</div>
    ])}/>
  </div>
}


function ContentOpportunities({ rows, showEvidence }) {
  return <div className="opportunity-list">{rows.length ? rows.map((o,i) => {
    const ids = String(o['Evidence IDs'] || '').split(',').map(x=>x.trim()).filter(Boolean)
    return <article className="card opportunity" key={`${o['Opportunity Type']}-${i}`}><div className="opp-head"><Badge tone={o['Opportunity Priority']}>{o['Opportunity Priority']}</Badge><Badge>{o['Recommendation Confidence'] || '—'}</Badge><Badge>{o['Source'] || 'deterministic'}</Badge></div><h3>{o['Topic / Keyword'] || o['Opportunity Type']}</h3><p><strong>{o['Opportunity Type']}</strong> · {o['Current Client State'] || 'Client review required'}</p><p>{o['Recommendation']}</p><small>{o['Benchmark Evidence'] || ''}</small><div className="evidence-links">{ids.map(id => <button className="link" key={id} onClick={()=>showEvidence(id)}>{id}</button>)}</div></article>
  }) : <p>No content opportunities detected.</p>}</div>
}

function DictTable({ rows, showEvidence }) {
  if (!rows.length) return <div className="card"><div className="empty-table">No data yet. Start a new analysis after applying the detailed-analysis patch.</div></div>
  const headers = Object.keys(rows[0])
  return <div className="card"><Table headers={headers} rows={rows.map(r => headers.map(h => {
    const value = r[h]
    if (h.toLowerCase().includes('evidence id') && value && showEvidence) return <button className="link" onClick={()=>showEvidence(value)}>{value}</button>
    return Array.isArray(value) ? value.join(' | ') : (value ?? '—')
  }))} /></div>
}

function Failures({ rows }) { return <div className="card"><Table headers={['Role','URL','HTTP','Terminal State','Retryable','Retries','Error']} rows={rows.map(r => [r.role,r.url,r.status_code ?? '—',r.terminal_state || '—',r.retryable == null ? '—' : (r.retryable ? 'Yes' : 'No'),r.retry_count,r.error])}/></div> }
function Table({headers,rows}) { return <div className="table-wrap"><table><thead><tr>{headers.map(h=><th key={h}>{h}</th>)}</tr></thead><tbody>{rows.map((row,i)=><tr key={i}>{row.map((c,j)=><td key={j}>{c}</td>)}</tr>)}</tbody></table>{!rows.length && <div className="empty-table">No data yet.</div>}</div> }
function EmptyState() { return <div className="empty card"><h2>Create a project</h2><p>Add one client URL, P0/P1 benchmark URLs and target keyword phrases. Then start analysis.</p></div> }

function NewProjectDialog({ projects, onCreated }) {
  const [name, setName] = useState('')
  const [client, setClient] = useState('')
  const [parentProjectId, setParentProjectId] = useState('')

  const submit = async e => {
    e.preventDefault()
    const p = await api.createProject({
      name,
      client_name: client || null,
      parent_project_id: parentProjectId ? Number(parentProjectId) : null,
    })
    setName('')
    setClient('')
    setParentProjectId('')
    document.getElementById('new-project').close()
    await onCreated(p)
  }

  return <dialog id="new-project">
    <form onSubmit={e => submit(e).catch(err => alert(err.message || err))}>
      <h2>New project</h2>
      <label>
        Project name
        <input required value={name} onChange={e => setName(e.target.value)} />
      </label>
      <label>
        Client name
        <input value={client} onChange={e => setClient(e.target.value)} />
      </label>
      <label>
        Parent project (optional)
        <select value={parentProjectId} onChange={e => setParentProjectId(e.target.value)}>
          <option value="">Root project</option>
          {flattenProjects(projects).map(p => (
            <option key={p.id} value={p.id}>
              {`${'— '.repeat(p.depth)}${p.name}`}
            </option>
          ))}
        </select>
      </label>
      <div className="dialog-actions">
        <button type="button" onClick={() => document.getElementById('new-project').close()}>Cancel</button>
        <button className="primary">Create</button>
      </div>
    </form>
  </dialog>
}


function ProjectManagerDialog({
  project,
  projects,
  onChanged,
  onSubprojectCreated,
}) {
  const [name, setName] = useState(project.name || '')
  const [client, setClient] = useState(project.client_name || '')
  const [parentProjectId, setParentProjectId] = useState(
    project.parent_project_id == null ? '' : String(project.parent_project_id)
  )
  const [childName, setChildName] = useState('')
  const [childClient, setChildClient] = useState('')
  const [error, setError] = useState('')

  const save = async e => {
    e.preventDefault()
    setError('')
    try {
      const updated = await api.updateProject(project.id, {
        name,
        client_name: client || null,
        parent_project_id: parentProjectId ? Number(parentProjectId) : null,
      })
      document.getElementById('manage-project').close()
      await onChanged(updated)
    } catch (e) {
      setError(e.message)
    }
  }

  const addSubproject = async () => {
    if (!childName.trim()) return
    setError('')
    try {
      const child = await api.createSubproject(project.id, {
        name: childName.trim(),
        client_name: childClient || null,
      })
      setChildName('')
      setChildClient('')
      document.getElementById('manage-project').close()
      await onSubprojectCreated(child)
    } catch (e) {
      setError(e.message)
    }
  }

  return <dialog id="manage-project">
    <form onSubmit={save}>
      <h2>Manage project</h2>

      {error && <div className="alert error">{error}</div>}

      <label>
        Project name
        <input required value={name} onChange={e => setName(e.target.value)} />
      </label>

      <label>
        Client name
        <input value={client} onChange={e => setClient(e.target.value)} />
      </label>

      <label>
        Parent project
        <select value={parentProjectId} onChange={e => setParentProjectId(e.target.value)}>
          <option value="">Root project</option>
          {flattenProjects(projects)
            .filter(p => Number(p.id) !== Number(project.id))
            .map(p => (
              <option key={p.id} value={p.id}>
                {`${'— '.repeat(p.depth)}${p.name}`}
              </option>
            ))}
        </select>
      </label>

      <div className="project-manager-divider" />

      <h3>Add subproject</h3>
      <label>
        Subproject name
        <input value={childName} onChange={e => setChildName(e.target.value)} />
      </label>
      <label>
        Subproject client name
        <input value={childClient} onChange={e => setChildClient(e.target.value)} />
      </label>
      <button type="button" className="secondary-light" onClick={addSubproject}>
        Create subproject
      </button>

      <div className="dialog-actions">
        <button type="button" onClick={() => document.getElementById('manage-project').close()}>Close</button>
        <button className="primary">Save project</button>
      </div>
    </form>
  </dialog>
}
