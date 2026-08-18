import { useEffect, useState } from 'react'
import { listAuditLog, listRuns, rollbackRow } from '../api/client'
import type { AuditLogEntry, Domain, EvaluationRun, ScenarioDataset } from '../types'

interface Props {
  domain: Domain
  datasetId: number | null
  onBack: () => void
  onRolledBack: (dataset: ScenarioDataset) => void
}

const ACTION_LABELS: Record<string, string> = {
  'dataset.uploaded': 'Uploaded dataset',
  'dataset.manual_created': 'Created blank dataset',
  'dataset.deleted': 'Deleted dataset',
  'row.added': 'Added row',
  'row.updated': 'Edited row',
  'row.deleted': 'Deleted row',
  'row.rolled_back': 'Restored row',
  'run.started': 'Started RAGAS run',
  'run.completed': 'RAGAS run completed',
  'run.failed': 'RAGAS run failed',
  'run.cancelled': 'RAGAS run stopped',
}

const ROLLBACKABLE_ACTIONS = new Set(['row.updated', 'row.added', 'row.deleted'])

function describeChange(entry: AuditLogEntry): string {
  if (entry.action === 'row.updated' && entry.before && entry.after) {
    const before = entry.before as { test_scenario?: string; expected_result?: string }
    const after = entry.after as { test_scenario?: string; expected_result?: string }
    const parts: string[] = []
    if (before.test_scenario !== after.test_scenario) {
      parts.push(`Test Scenario: "${before.test_scenario}" → "${after.test_scenario}"`)
    }
    if (before.expected_result !== after.expected_result) {
      parts.push(`Expected Result: "${before.expected_result}" → "${after.expected_result}"`)
    }
    return parts.join('; ')
  }
  if (entry.action === 'row.added' && entry.after) {
    const after = entry.after as { test_scenario?: string }
    return `"${after.test_scenario}"`
  }
  if (entry.action === 'row.deleted' && entry.before) {
    const before = entry.before as { test_scenario?: string }
    return `"${before.test_scenario}"`
  }
  if (entry.action === 'row.rolled_back') {
    const after = entry.after as { test_scenario?: string; restored_from_audit_log_id?: number } | null
    const before = entry.before as { test_scenario?: string } | null
    const label = after?.test_scenario ?? before?.test_scenario ?? ''
    return `"${label}" (from log #${after?.restored_from_audit_log_id ?? '?'})`
  }
  if (entry.action === 'dataset.uploaded' && entry.after) {
    const after = entry.after as { source_filename?: string; row_count?: number }
    return `${after.source_filename} (${after.row_count} rows)`
  }
  if (entry.action === 'dataset.manual_created' && entry.after) {
    const after = entry.after as { source_filename?: string }
    return `${after.source_filename}`
  }
  if (entry.action === 'dataset.deleted' && entry.before) {
    const before = entry.before as { source_filename?: string; row_count?: number }
    return `${before.source_filename} (${before.row_count} rows)`
  }
  if (entry.action === 'run.completed' && entry.after) {
    const after = entry.after as { summary?: Record<string, number> }
    const summary = after.summary ?? {}
    return Object.entries(summary)
      .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v.toFixed(2)}`)
      .join(', ')
  }
  if ((entry.action === 'run.failed' || entry.action === 'run.cancelled') && entry.after) {
    const after = entry.after as { error?: string }
    return after.error ?? ''
  }
  if (entry.action === 'run.started' && entry.after) {
    const after = entry.after as { row_count?: number }
    return `${after.row_count} scenarios queued`
  }
  return ''
}

export default function AuditLogPanel({ domain, datasetId, onBack, onRolledBack }: Props) {
  const [entries, setEntries] = useState<AuditLogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [onlyCurrent, setOnlyCurrent] = useState(datasetId != null)
  const [restoringId, setRestoringId] = useState<number | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [runs, setRuns] = useState<EvaluationRun[]>([])
  const [runsLoading, setRunsLoading] = useState(false)

  const filterId = onlyCurrent ? (datasetId ?? undefined) : undefined

  // Calculate version numbers based on chronological order of runs
  const chronological = [...runs].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  )
  const versionOf = (id: number) => chronological.findIndex((r) => r.id === id) + 1

  const load = () => {
    setLoading(true)
    setError(null)
    listAuditLog(domain, filterId)
      .then(setEntries)
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false))
  }

  // Load runs to calculate versions (only when filtering by current dataset)
  useEffect(() => {
    if (datasetId == null || !onlyCurrent) {
      setRuns([])
      setRunsLoading(false)
      return
    }
    setRunsLoading(true)
    listRuns(datasetId)
      .then(setRuns)
      .catch(() => setRuns([]))
      .finally(() => setRunsLoading(false))
  }, [datasetId, onlyCurrent])

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain, filterId])

  const handleRollback = async (entry: AuditLogEntry) => {
    if (entry.dataset_id == null) return
    setRestoringId(entry.id)
    setError(null)
    setNotice(null)
    try {
      const dataset = await rollbackRow(entry.dataset_id, entry.id)
      onRolledBack(dataset)
      setNotice(`Restored the row changed in log #${entry.id}.`)
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setRestoringId(null)
    }
  }

  return (
    <div className="run-panel">
      <div className="scenario-table-toolbar">
        <button className="secondary" onClick={onBack}>
          ← Back to editor
        </button>
        {datasetId != null && (
          <label className="audit-scope-toggle">
            <input
              type="checkbox"
              checked={onlyCurrent}
              onChange={(e) => setOnlyCurrent(e.target.checked)}
            />
            Only this scenario set
          </label>
        )}
      </div>

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner success">{notice}</div>}
      {loading && <p>Loading audit log…</p>}

      {!loading && !error && (
        <div style={{ overflowX: 'auto' }}>
          <table className="run-results-table audit-log-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Scenario Set</th>
                <th>Run</th>
                <th>Details</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => {
                const when = new Date(entry.created_at)
                const canRollback = ROLLBACKABLE_ACTIONS.has(entry.action) && entry.dataset_id != null
                return (
                  <tr key={entry.id}>
                    <td>
                      <div className="audit-when-date">
                        {when.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}
                      </div>
                      <div className="audit-when-time">
                        {when.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
                      </div>
                    </td>
                    <td>{entry.actor_email}</td>
                    <td>
                      <span className={`audit-action-chip action-${entry.action.split('.')[0]}`}>
                        {ACTION_LABELS[entry.action] ?? entry.action}
                      </span>
                    </td>
                    <td>
                      {entry.dataset_name ? (
                        <span className="audit-dataset-chip">{entry.dataset_name}</span>
                      ) : entry.dataset_id != null ? (
                        <span className="audit-dataset-chip deleted">deleted set #{entry.dataset_id}</span>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td>
                      {entry.run_id != null ? (
                        <span className="audit-run-chip">
                          #{entry.run_id}
                          {runs.some((r) => r.id === entry.run_id) && (
                            <span className="audit-run-version"> v{versionOf(entry.run_id)}</span>
                          )}
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td>{describeChange(entry)}</td>
                    <td>
                      {canRollback && (
                        <button
                          className="secondary rollback-button"
                          onClick={() => handleRollback(entry)}
                          disabled={restoringId === entry.id}
                        >
                          {restoringId === entry.id ? 'Restoring…' : 'Restore'}
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
              {entries.length === 0 && (
                <tr>
                  <td colSpan={7} className="empty-state" style={{ textAlign: 'center', padding: '16px' }}>
                    No changes logged yet{onlyCurrent ? ' for this scenario set' : ' for this domain'}.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
