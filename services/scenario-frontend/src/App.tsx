import { useEffect, useState } from 'react'
import {
  addRow,
  createManualDataset,
  deleteDataset,
  deleteRow,
  getDataset,
  listDatasets,
  listRuns,
  saveRows,
  startRun,
  uploadDataset,
} from './api/client'
import AuditLogPanel from './components/AuditLogPanel'
import DatasetPicker from './components/DatasetPicker'
import RunCompare from './components/RunCompare'
import RunResults from './components/RunResults'
import ScenarioTable from './components/ScenarioTable'
import UploadForm from './components/UploadForm'
import LoginPage from './pages/LoginPage'
import type {
  Domain,
  EvaluationRun,
  ScenarioDataset,
  ScenarioDatasetSummary,
  ScenarioRowUpdate,
  Session,
} from './types'

const DOMAIN_LABELS: Record<Domain, string> = {
  hr: 'HR',
  contact_center: 'Contact Center',
}

const SESSION_KEY = 'scenario-manager-session'

function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY)
    return raw ? (JSON.parse(raw) as Session) : null
  } catch {
    return null
  }
}

export default function App() {
  const [session, setSession] = useState<Session | null>(() => loadSession())
  const [domain, setDomain] = useState<Domain>('hr')
  const [datasets, setDatasets] = useState<ScenarioDatasetSummary[]>([])
  const [current, setCurrent] = useState<ScenarioDataset | null>(null)
  const [uploading, setUploading] = useState(false)
  const [creatingManual, setCreatingManual] = useState(false)
  const [addingRow, setAddingRow] = useState(false)
  const [saving, setSaving] = useState(false)
  const [deletingDataset, setDeletingDataset] = useState(false)
  const [startingRun, setStartingRun] = useState(false)
  const [activeRunId, setActiveRunId] = useState<number | null>(null)
  const [view, setView] = useState<'editor' | 'audit' | 'compare'>('editor')
  const [lastRun, setLastRun] = useState<EvaluationRun | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [roleHint, setRoleHint] = useState<string | null>(null)

  const handleLoggedIn = (s: Session) => {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(s))
    setSession(s)
    if (s.roles.includes('admin')) {
      setRoleHint('Admin can manage both HR and Contact Center domains.')
    } else if (s.roles.some((role) => role.startsWith('hr_')) || s.roles.includes('hr_user')) {
      setRoleHint('HR users can only manage the HR domain.')
    } else if (s.roles.some((role) => role.startsWith('cc_')) || s.roles.includes('cc_user')) {
      setRoleHint('Contact Center users can only manage the Contact Center domain.')
    } else {
      setRoleHint(null)
    }
  }

  const handleLogout = () => {
    sessionStorage.removeItem(SESSION_KEY)
    setSession(null)
  }

  const refreshDatasets = async (d: Domain, selectId?: number) => {
    const list = await listDatasets(d)
    setDatasets(list)
    if (selectId != null) {
      setCurrent(await getDataset(selectId))
    } else if (list.length > 0) {
      setCurrent(await getDataset(list[0].id))
    } else {
      setCurrent(null)
    }
  }

  useEffect(() => {
    if (!session) return
    setError(null)
    setActiveRunId(null)
    setView('editor')
    refreshDatasets(domain).catch((e) => setError((e as Error).message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain, session])

  // Tracks the most recent run for whichever dataset is selected, so "View Last
  // Result" can jump straight to it without starting a new (costly) run. Re-checks
  // whenever the selected dataset changes, or when returning from the run view
  // (activeRunId going back to null) in case a run just finished.
  useEffect(() => {
    if (!current) {
      setLastRun(null)
      return
    }
    listRuns(current.id)
      .then((runs) => setLastRun(runs[0] ?? null))
      .catch(() => setLastRun(null))
  }, [current?.id, activeRunId])

  useEffect(() => {
    if (!session) return

    const isAdmin = session.roles.includes('admin')
    const isHr = session.roles.some((role) => role.startsWith('hr_') || role === 'hr_user')
    const isCc = session.roles.some((role) => role.startsWith('cc_') || role === 'cc_user')

    if (!isAdmin) {
      if (isHr && !isCc) {
        setDomain('hr')
      } else if (isCc && !isHr) {
        setDomain('contact_center')
      }
    }
  }, [session])

  if (!session) {
    return <LoginPage onLoggedIn={handleLoggedIn} />
  }

  // The local admin bypass account (see app/platform_client.py _LOCAL_USERS) has no
  // real AI Platform session behind it, so it can browse/manage scenarios but can
  // never run RAGAS — surfaced here up front instead of only after a failed click.
  const isLocalAdmin = session.email === 'admin@example.com'

  const handleUpload = async (file: File) => {
    setUploading(true)
    setError(null)
    setMessage(null)
    try {
      const dataset = await uploadDataset(file, domain)
      await refreshDatasets(domain, dataset.id)
      setMessage(`Uploaded "${dataset.source_filename}" — ${dataset.rows.length} rows`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUploading(false)
    }
  }

  const handleStartManual = async () => {
    setCreatingManual(true)
    setError(null)
    setMessage(null)
    try {
      const dataset = await createManualDataset(domain)
      await refreshDatasets(domain, dataset.id)
      setMessage('New blank scenario set ready — add rows below.')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setCreatingManual(false)
    }
  }

  const handleSelectDataset = async (id: number) => {
    setError(null)
    setMessage(null)
    try {
      setCurrent(await getDataset(id))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleDeleteDataset = async (id: number) => {
    setDeletingDataset(true)
    setError(null)
    setMessage(null)
    try {
      await deleteDataset(id)
      await refreshDatasets(domain)
      setMessage('Scenario set deleted.')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setDeletingDataset(false)
    }
  }

  const handleSave = async (rows: ScenarioRowUpdate[]) => {
    if (!current) return
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      setCurrent(await saveRows(current.id, rows))
      setMessage('Saved.')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const handleAddRow = async () => {
    if (!current) return
    setAddingRow(true)
    setError(null)
    try {
      setCurrent(await addRow(current.id, { test_scenario: '', expected_result: '' }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setAddingRow(false)
    }
  }

  const handleDeleteRow = async (rowId: number) => {
    if (!current) return
    setError(null)
    try {
      setCurrent(await deleteRow(current.id, rowId))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleRunRagas = async () => {
    if (!current) return
    setStartingRun(true)
    setError(null)
    setMessage(null)
    try {
      const run = await startRun(current.id, session.access_token)
      setActiveRunId(run.id)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setStartingRun(false)
    }
  }

  const handleViewLastRun = () => {
    if (!lastRun) return
    setActiveRunId(lastRun.id)
  }

  return (
    <div className="app-shell">
      <main className="workspace">
        <header className="workspace-header">
          <div className="workspace-brand">RAGAS Evaluation</div>
          <button className="link-button" onClick={handleLogout}>
            Log out
          </button>
        </header>

        <div className="main-surface">
          {activeRunId != null ? (
            <div className="content-card">
              <RunResults
                runId={activeRunId}
                accessToken={session.access_token}
                onBack={() => setActiveRunId(null)}
              />
            </div>
          ) : view === 'audit' ? (
            <div className="content-card audit-panel">
              <AuditLogPanel
                domain={domain}
                datasetId={current?.id ?? null}
                onBack={() => setView('editor')}
                onRolledBack={(dataset) => {
                  setCurrent(dataset)
                  refreshDatasets(domain, dataset.id).catch((e) => setError((e as Error).message))
                }}
              />
            </div>
          ) : view === 'compare' && current ? (
            <div className="content-card">
              <RunCompare datasetId={current.id} onBack={() => setView('editor')} />
            </div>
          ) : (
            <div className="content-card workspace-panel">
              <div className="workspace-topbar">
                <div className="user-bubble">{session.email}</div>
                <button className="link-button" onClick={handleLogout}>
                  Log out
                </button>
              </div>

              {error && <div className="banner error">{error}</div>}
              {message && <div className="banner success">{message}</div>}
              {isLocalAdmin && (
                <div className="banner warn">
                  You're signed in as the local admin account — it can browse and edit scenarios, but can't
                  run RAGAS (no real AI Platform session). Log out and sign in with a real platform account
                  (e.g. hr_mgr_1@example.com / cc_mgr_1@example.com) to run evaluations.
                </div>
              )}

              <section className="controls">
                <div className="controls-section upload-section">
                  <UploadForm
                    domain={domain}
                    domainDisabled={!session.roles.includes('admin')}
                    onDomainChange={(selectedDomain) => {
                      const isAdmin = session?.roles.includes('admin')
                      if (!isAdmin) {
                        return
                      }
                      setDomain(selectedDomain)
                    }}
                    onUpload={handleUpload}
                    uploading={uploading}
                  />

                  <button className="secondary" onClick={handleStartManual} disabled={creatingManual}>
                    {creatingManual ? 'Creating…' : '+ Start Manual Entry'}
                  </button>
                </div>

                <div className="controls-section dataset-section">
                  <DatasetPicker
                    datasets={datasets}
                    selectedId={current?.id ?? null}
                    onSelect={handleSelectDataset}
                    onDelete={handleDeleteDataset}
                    deleting={deletingDataset}
                  />
                </div>

                <div className="controls-section action-buttons-section">
                  <button className="secondary" onClick={() => setView('audit')}>
                    Audit Log
                  </button>

                  {current && (
                    <button className="secondary" onClick={() => setView('compare')}>
                      Compare Runs
                    </button>
                  )}
                </div>

                {roleHint && <div className="role-hint">{roleHint}</div>}
              </section>

              {current ? (
                <ScenarioTable
                  rows={current.rows}
                  onSave={handleSave}
                  saving={saving}
                  onAddRow={handleAddRow}
                  addingRow={addingRow}
                  onDeleteRow={handleDeleteRow}
                  onRunRagas={handleRunRagas}
                  running={startingRun}
                  canRun={!isLocalAdmin}
                  lastRun={lastRun}
                  onViewLastRun={handleViewLastRun}
                />
              ) : (
                <p className="empty-state">
                  No scenarios yet for {DOMAIN_LABELS[domain]}. Upload a file or start manual entry above.
                </p>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
