import { useEffect, useRef, useState } from 'react'
import { cancelRun, getRun, getRunResults } from '../api/client'
import type { EvaluationRun, EvaluationRunResult } from '../types'

interface Props {
  runId: number
  accessToken: string
  onBack: () => void
}

const METRIC_ORDER = [
  'faithfulness',
  'answer_relevancy',
  'context_precision',
  'context_recall',
  'answer_correctness',
  'noise_sensitivity',
  'summarization_score',
]

export default function RunResults({ runId, accessToken, onBack }: Props) {
  const [run, setRun] = useState<EvaluationRun | null>(null)
  const [results, setResults] = useState<EvaluationRunResult[]>([])
  const [error, setError] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    const poll = async () => {
      try {
        const r = await getRun(runId)
        setRun(r)
        if (r.status === 'completed' || r.status === 'failed' || r.status === 'cancelled') {
          if (r.status === 'completed') {
            setResults(await getRunResults(runId))
          }
          if (intervalRef.current) clearInterval(intervalRef.current)
        }
      } catch (e) {
        setError((e as Error).message)
        if (intervalRef.current) clearInterval(intervalRef.current)
      }
    }

    poll()
    intervalRef.current = setInterval(poll, 3000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [runId])

  const isActive = run?.status === 'pending' || run?.status === 'running'

  const handleStop = async () => {
    setCancelling(true)
    setError(null)
    try {
      setRun(await cancelRun(runId, accessToken))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setCancelling(false)
    }
  }
  const metricNames = run?.summary
    ? [...METRIC_ORDER.filter((m) => m in run.summary!), ...Object.keys(run.summary).filter((m) => !METRIC_ORDER.includes(m))]
    : []

  return (
    <div className="run-panel">
      <div className="scenario-table-toolbar">
        <button className="secondary" onClick={onBack}>
          ← Back to editor
        </button>
        {isActive && (
          <button className="danger stop-button" onClick={handleStop} disabled={cancelling}>
            {cancelling ? 'Stopping…' : '■ Stop Run'}
          </button>
        )}
      </div>

      {error && <div className="banner error">{error}</div>}

      {run && (
        <div className="run-status-line">
          <span className={`run-status-badge ${run.status}`}>{run.status}</span>
          {isActive && <span className="spinner" aria-label="Running" />}
          <span>
            Run #{run.id} — {run.domain} — requested by {run.requested_by_email}
          </span>
        </div>
      )}

      {run?.status === 'failed' && <div className="banner error">{run.error_message}</div>}
      {run?.status === 'cancelled' && <div className="banner warn">Run stopped by user.</div>}

      {run?.status === 'completed' && run.summary && (
        <div className="run-summary">
          {metricNames.map((name) => (
            <div className="metric-tile" key={name}>
              <div className="metric-name">{name.replace(/_/g, ' ')}</div>
              <div className="metric-value">{run.summary![name].toFixed(2)}</div>
            </div>
          ))}
        </div>
      )}

      {run?.status === 'completed' && (
        <div style={{ overflowX: 'auto' }}>
          <table className="run-results-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Question</th>
                <th>Answer</th>
                <th>Ground Truth</th>
                {metricNames.map((name) => (
                  <th key={name}>{name.replace(/_/g, ' ')}</th>
                ))}
                <th>Latency (s)</th>
              </tr>
            </thead>
            <tbody>
              {results.map((r, i) => (
                <tr key={r.id}>
                  <td>{i + 1}</td>
                  <td>{r.question}</td>
                  <td>{r.answer}</td>
                  <td>{r.ground_truth}</td>
                  {metricNames.map((name) => (
                    <td key={name}>{name in r.metrics ? r.metrics[name].toFixed(2) : '—'}</td>
                  ))}
                  <td>{r.latency_seconds.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
