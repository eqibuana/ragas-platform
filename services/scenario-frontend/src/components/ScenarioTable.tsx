import { useEffect, useState } from 'react'
import type { EvaluationRun, ScenarioRow, ScenarioRowUpdate } from '../types'

interface Props {
  rows: ScenarioRow[]
  onSave: (rows: ScenarioRowUpdate[]) => Promise<void>
  saving: boolean
  onAddRow: () => Promise<void>
  addingRow: boolean
  onDeleteRow: (rowId: number) => Promise<void>
  onRunRagas: () => Promise<void>
  running: boolean
  canRun: boolean
  lastRun: EvaluationRun | null
  onViewLastRun: () => void
}

export default function ScenarioTable({
  rows,
  onSave,
  saving,
  onAddRow,
  addingRow,
  onDeleteRow,
  onRunRagas,
  running,
  canRun,
  lastRun,
  onViewLastRun,
}: Props) {
  const [edited, setEdited] = useState<ScenarioRow[]>(rows)
  const [deletingId, setDeletingId] = useState<number | null>(null)

  useEffect(() => {
    setEdited(rows)
  }, [rows])

  const isDirty = edited.some((row, i) => {
    const original = rows[i]
    return (
      !original ||
      row.test_scenario !== original.test_scenario ||
      row.expected_result !== original.expected_result
    )
  })

  const updateCell = (id: number, field: 'test_scenario' | 'expected_result', value: string) => {
    setEdited((prev) => prev.map((row) => (row.id === id ? { ...row, [field]: value } : row)))
  }

  const handleSave = () => {
    onSave(edited.map(({ id, test_scenario, expected_result }) => ({ id, test_scenario, expected_result })))
  }

  const handleDelete = async (rowId: number) => {
    setDeletingId(rowId)
    try {
      await onDeleteRow(rowId)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="scenario-table-wrapper">
      <div className="scenario-table-toolbar">
        <button onClick={handleSave} disabled={!isDirty || saving}>
          {saving ? 'Saving…' : 'Save Changes'}
        </button>
        {isDirty && !saving && <span className="dirty-hint">Unsaved changes</span>}
        <button className="secondary" onClick={onAddRow} disabled={addingRow}>
          {addingRow ? 'Adding…' : '+ Add Row'}
        </button>
        <button
          className="run-button"
          onClick={onRunRagas}
          disabled={running || isDirty || rows.length === 0 || !canRun}
          title={
            !canRun
              ? 'Local admin has no real AI Platform session — log in with a real account to run RAGAS'
              : isDirty
                ? 'Save changes before running'
                : undefined
          }
        >
          {running ? 'Starting run…' : 'Run RAGAS'}
        </button>
        {lastRun && (
          <button className="secondary" onClick={onViewLastRun}>
            View Last Result
            <span className={`run-status-badge ${lastRun.status}`} style={{ marginLeft: 8 }}>
              {lastRun.status}
            </span>
          </button>
        )}
      </div>
      <table className="scenario-table">
        <thead>
          <tr>
            <th className="row-num">#</th>
            <th>Test Scenario</th>
            <th>Expected Result</th>
            <th className="row-actions"></th>
          </tr>
        </thead>
        <tbody>
          {edited.map((row, i) => (
            <tr key={row.id}>
              <td className="row-num">{i + 1}</td>
              <td>
                <textarea
                  value={row.test_scenario}
                  onChange={(e) => updateCell(row.id, 'test_scenario', e.target.value)}
                />
              </td>
              <td>
                <textarea
                  value={row.expected_result}
                  onChange={(e) => updateCell(row.id, 'expected_result', e.target.value)}
                />
              </td>
              <td className="row-actions">
                <button
                  className="danger-icon"
                  onClick={() => handleDelete(row.id)}
                  disabled={deletingId === row.id}
                >
                  {deletingId === row.id ? '…' : 'Delete'}
                </button>
              </td>
            </tr>
          ))}
          {edited.length === 0 && (
            <tr>
              <td colSpan={4} className="empty-state" style={{ textAlign: 'center', padding: '16px' }}>
                No rows yet — upload a file or add one manually.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
