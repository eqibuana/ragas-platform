export type Domain = 'hr' | 'contact_center'

export interface ScenarioRow {
  id: number
  row_index: number
  test_scenario: string
  expected_result: string
  updated_at: string
}

export interface ScenarioDataset {
  id: number
  domain: Domain
  source_filename: string
  uploaded_at: string
  rows: ScenarioRow[]
}

export interface ScenarioDatasetSummary {
  id: number
  domain: Domain
  source_filename: string
  uploaded_at: string
  row_count: number
}

export interface ScenarioRowUpdate {
  id: number
  test_scenario: string
  expected_result: string
}

export interface Session {
  access_token: string
  user_id: string
  email: string
  roles: string[]
}

export type RunStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface EvaluationRun {
  id: number
  dataset_id: number
  domain: Domain
  requested_by_email: string
  status: RunStatus
  error_message: string | null
  summary: Record<string, number> | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface EvaluationRunResult {
  id: number
  scenario_row_id: number | null
  question: string
  ground_truth: string
  answer: string
  contexts: string
  metrics: Record<string, number>
  latency_seconds: number
}

export interface AuditLogEntry {
  id: number
  created_at: string
  actor_email: string
  actor_roles: string[]
  action: string
  domain: Domain | null
  dataset_id: number | null
  dataset_name: string | null
  entity_type: string | null
  entity_id: number | null
  run_id: number | null
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
}

export interface MetricDelta {
  metric: string
  run_a: number | null
  run_b: number | null
  delta: number | null
}

export interface QuestionDelta {
  scenario_row_id: number | null
  question: string
  run_a_metrics: Record<string, number> | null
  run_b_metrics: Record<string, number> | null
  metric_deltas: Record<string, number>
}

export interface RunCompareResult {
  run_a: EvaluationRun
  run_b: EvaluationRun
  summary_deltas: MetricDelta[]
  question_deltas: QuestionDelta[]
}
