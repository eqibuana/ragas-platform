import { useEffect, useRef, useState } from 'react'
import { compareRuns, listRuns } from '../api/client'
import type { EvaluationRun, RunCompareResult } from '../types'

interface Props {
  datasetId: number
  onBack: () => void
}

function deltaClass(delta: number | null): string {
  if (delta == null || delta === 0) return ''
  return delta > 0 ? 'delta-positive' : 'delta-negative'
}

function formatDelta(delta: number | null): string {
  if (delta == null) return '—'
  const sign = delta > 0 ? '+' : ''
  return `${sign}${delta.toFixed(4)}`
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

interface VersionSelectProps {
  label: string
  runs: EvaluationRun[]
  versionOf: (id: number) => number
  selectedId: number | null
  onSelect: (id: number) => void
}

function VersionSelect({ label, runs, versionOf, selectedId, onSelect }: VersionSelectProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const selected = runs.find((r) => r.id === selectedId) ?? null

  useEffect(() => {
    if (!open) return
    const onClickOutside = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [open])

  return (
    <div className="version-select" ref={rootRef}>
      <span className="version-select-label">{label}</span>
      <button type="button" className="version-select-trigger" onClick={() => setOpen((o) => !o)}>
        {selected ? (
          <span className="version-option-content">
            <span className="version-tag">v{versionOf(selected.id)}</span>
            <span className="version-option-meta">
              <span className="version-option-date">
                {formatDate(selected.created_at)} · {formatTime(selected.created_at)}
              </span>
            </span>
          </span>
        ) : (
          <span className="version-option-content">Select a run…</span>
        )}
        <span className={`dataset-select-chevron ${open ? 'open' : ''}`}>▾</span>
      </button>
      {open && (
        <div className="version-select-menu">
          {runs.map((r) => (
            <button
              type="button"
              key={r.id}
              className={`version-option ${r.id === selectedId ? 'selected' : ''}`}
              onClick={() => {
                onSelect(r.id)
                setOpen(false)
              }}
            >
              <span className="version-tag">v{versionOf(r.id)}</span>
              <span className="version-option-meta">
                <span className="version-option-date">
                  {formatDate(r.created_at)} · {formatTime(r.created_at)}
                </span>
              </span>
              <span className="run-status-badge completed">{r.status}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default function RunCompare({ datasetId, onBack }: Props) {
  const [runs, setRuns] = useState<EvaluationRun[]>([])
  const [runAId, setRunAId] = useState<number | null>(null)
  const [runBId, setRunBId] = useState<number | null>(null)
  const [result, setResult] = useState<RunCompareResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    listRuns(datasetId).then((all) => {
      const completed = all.filter((r) => r.status === 'completed')
      setRuns(completed)
      if (completed.length >= 2) {
        setRunAId(completed[1].id)
        setRunBId(completed[0].id)
      }
    })
  }, [datasetId])

  useEffect(() => {
    if (runAId == null || runBId == null || runAId === runBId) {
      setResult(null)
      return
    }
    setLoading(true)
    setError(null)
    compareRuns(runAId, runBId)
      .then(setResult)
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false))
  }, [runAId, runBId])

  // Chronological order (oldest first) so "v1" always means the earliest run on this
  // dataset, stable regardless of which two runs are currently selected for comparison.
  const chronological = [...runs].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  )
  const versionOf = (id: number) => chronological.findIndex((r) => r.id === id) + 1
  const versionA = runAId != null ? versionOf(runAId) : null
  const versionB = runBId != null ? versionOf(runBId) : null

  return (
    <div className="run-panel">
      <div className="scenario-table-toolbar">
        <button className="secondary" onClick={onBack}>
          ← Back to editor
        </button>
      </div>

      {runs.length < 2 ? (
        <p className="empty-state">Need at least two completed runs on this dataset to compare.</p>
      ) : (
        <>
          <div className="version-select-row">
            <VersionSelect
              label="Baseline"
              runs={runs}
              versionOf={versionOf}
              selectedId={runAId}
              onSelect={setRunAId}
            />
            <span className="version-arrow">→</span>
            <VersionSelect
              label="Compare to"
              runs={runs}
              versionOf={versionOf}
              selectedId={runBId}
              onSelect={setRunBId}
            />
          </div>

          {error && <div className="banner error">{error}</div>}
          {loading && <p>Comparing…</p>}

          {result && (
            <>
              <div className="run-summary">
                {result.summary_deltas.map((d) => (
                  <div className="metric-tile" key={d.metric}>
                    <div className="metric-name">{d.metric.replace(/_/g, ' ')}</div>
                    <div className="metric-value">
                      {d.run_a?.toFixed(2) ?? '—'} → {d.run_b?.toFixed(2) ?? '—'}
                    </div>
                    <div className={deltaClass(d.delta)}>{formatDelta(d.delta)}</div>
                  </div>
                ))}
              </div>

              <div style={{ overflowX: 'auto' }}>
                <table className="run-results-table">
                  <thead>
                    <tr>
                      <th>Question</th>
                      {Object.keys(result.question_deltas[0]?.metric_deltas ?? {}).map((m) => (
                        <th key={m}>
                          {m.replace(/_/g, ' ')} Δ{' '}
                          <span className="version-delta-tag">
                            v{versionA}→v{versionB}
                          </span>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.question_deltas.map((q) => (
                      <tr key={`${q.scenario_row_id}-${q.question}`}>
                        <td>{q.question}</td>
                        {Object.keys(result.question_deltas[0]?.metric_deltas ?? {}).map((m) => (
                          <td key={m} className={deltaClass(q.metric_deltas[m] ?? null)}>
                            {formatDelta(q.metric_deltas[m] ?? null)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
    </div>
  )
}
