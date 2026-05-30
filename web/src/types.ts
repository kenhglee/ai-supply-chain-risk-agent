export interface TraceSummary {
  alert_id: string | null;
  trace_id: string | null;
  headline: string | null;
  final_status: string | null;
  created_at: string | null;
  run_duration_ms: number | null;
}

export interface TraceStep {
  node_name: string;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  decision?: string;
  error?: string;
}

export interface TraceDetail {
  alert_id: string | null;
  trace_id: string | null;
  headline: string | null;
  final_status: string | null;
  created_at: string | null;
  run_duration_ms: number | null;
  tool_decision: string | null;
  supplier: string | null;
  risk_type: string | null;
  risk_level: string | null;
  change_type: string | null;
  trace_steps: TraceStep[];
}
