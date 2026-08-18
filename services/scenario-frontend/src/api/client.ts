import type {
  AuditLogEntry,
  Domain,
  EvaluationRun,
  EvaluationRunResult,
  RunCompareResult,
  ScenarioDataset,
  ScenarioDatasetSummary,
  ScenarioRowUpdate,
  Session,
} from '../types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
const SESSION_KEY = 'scenario-manager-session'

function authHeaders(): Record<string, string> | undefined {
  const raw = sessionStorage.getItem(SESSION_KEY)
  if (!raw) return undefined
  try {
    const session = JSON.parse(raw) as Session
    return { Authorization: `Bearer ${session.access_token}` }
  } catch {
    return undefined
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = res.statusText
    try {
      const body = await res.json()
      message = body.detail ?? message
    } catch {
      // response body wasn't JSON — keep the statusText fallback
    }
    throw new Error(message)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export async function uploadDataset(file: File, domain: Domain): Promise<ScenarioDataset> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('domain', domain)
  const res = await fetch(`${API_BASE_URL}/api/datasets`, {
    method: 'POST',
    headers: authHeaders(),
    body: formData,
  })
  return handleResponse<ScenarioDataset>(res)
}

export async function listDatasets(domain: Domain): Promise<ScenarioDatasetSummary[]> {
  const res = await fetch(`${API_BASE_URL}/api/datasets?domain=${domain}`, {
    headers: authHeaders(),
  })
  return handleResponse<ScenarioDatasetSummary[]>(res)
}

export async function getDataset(id: number): Promise<ScenarioDataset> {
  const res = await fetch(`${API_BASE_URL}/api/datasets/${id}`, {
    headers: authHeaders(),
  })
  return handleResponse<ScenarioDataset>(res)
}

export async function saveRows(datasetId: number, rows: ScenarioRowUpdate[]): Promise<ScenarioDataset> {
  const res = await fetch(`${API_BASE_URL}/api/datasets/${datasetId}/rows`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...(authHeaders() ?? {}) },
    body: JSON.stringify(rows),
  })
  return handleResponse<ScenarioDataset>(res)
}

export async function deleteDataset(id: number): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/api/datasets/${id}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  await handleResponse<void>(res)
}

export async function createManualDataset(domain: Domain, name?: string): Promise<ScenarioDataset> {
  const res = await fetch(`${API_BASE_URL}/api/datasets/manual`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(authHeaders() ?? {}) },
    body: JSON.stringify({ domain, ...(name ? { name } : {}) }),
  })
  return handleResponse<ScenarioDataset>(res)
}

export async function addRow(
  datasetId: number,
  row: { test_scenario: string; expected_result: string },
): Promise<ScenarioDataset> {
  const res = await fetch(`${API_BASE_URL}/api/datasets/${datasetId}/rows`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(authHeaders() ?? {}) },
    body: JSON.stringify(row),
  })
  return handleResponse<ScenarioDataset>(res)
}

export async function deleteRow(datasetId: number, rowId: number): Promise<ScenarioDataset> {
  const res = await fetch(`${API_BASE_URL}/api/datasets/${datasetId}/rows/${rowId}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  return handleResponse<ScenarioDataset>(res)
}

export async function rollbackRow(datasetId: number, auditLogId: number): Promise<ScenarioDataset> {
  const res = await fetch(`${API_BASE_URL}/api/datasets/${datasetId}/rows/rollback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(authHeaders() ?? {}) },
    body: JSON.stringify({ audit_log_id: auditLogId }),
  })
  return handleResponse<ScenarioDataset>(res)
}

export async function login(email: string, password: string): Promise<Session> {
  const res = await fetch(`${API_BASE_URL}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  return handleResponse<Session>(res)
}

export async function startRun(datasetId: number, accessToken: string): Promise<EvaluationRun> {
  const res = await fetch(`${API_BASE_URL}/api/runs`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({ dataset_id: datasetId }),
  })
  return handleResponse<EvaluationRun>(res)
}

export async function getRun(runId: number): Promise<EvaluationRun> {
  const res = await fetch(`${API_BASE_URL}/api/runs/${runId}`)
  return handleResponse<EvaluationRun>(res)
}

export async function cancelRun(runId: number, accessToken: string): Promise<EvaluationRun> {
  const res = await fetch(`${API_BASE_URL}/api/runs/${runId}/cancel`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  return handleResponse<EvaluationRun>(res)
}

export async function listRuns(datasetId: number): Promise<EvaluationRun[]> {
  const res = await fetch(`${API_BASE_URL}/api/runs?dataset_id=${datasetId}`)
  return handleResponse<EvaluationRun[]>(res)
}

export async function getRunResults(runId: number): Promise<EvaluationRunResult[]> {
  const res = await fetch(`${API_BASE_URL}/api/runs/${runId}/results`)
  return handleResponse<EvaluationRunResult[]>(res)
}

export async function compareRuns(runA: number, runB: number): Promise<RunCompareResult> {
  const res = await fetch(`${API_BASE_URL}/api/runs/compare?run_a=${runA}&run_b=${runB}`, {
    headers: authHeaders(),
  })
  return handleResponse<RunCompareResult>(res)
}

export async function listAuditLog(domain: Domain, datasetId?: number): Promise<AuditLogEntry[]> {
  const params = new URLSearchParams({ domain })
  if (datasetId != null) params.set('dataset_id', String(datasetId))
  const res = await fetch(`${API_BASE_URL}/api/audit?${params.toString()}`, {
    headers: authHeaders(),
  })
  return handleResponse<AuditLogEntry[]>(res)
}
