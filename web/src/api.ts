import {
  TraceSummarySchema,
  TraceDetailSchema,
  ExplanationResponseSchema,
} from './types';
import type { TraceSummary, TraceDetail } from './types';

const BASE = 'http://localhost:8000';

export async function fetchTraces(): Promise<TraceSummary[]> {
  const res = await fetch(`${BASE}/api/traces`);
  if (!res.ok) throw new Error(`Failed to fetch traces (${res.status})`);
  return TraceSummarySchema.array().parse(await res.json());
}

export async function fetchTraceDetail(identifier: string): Promise<TraceDetail> {
  const res = await fetch(`${BASE}/api/traces/${encodeURIComponent(identifier)}`);
  if (!res.ok) throw new Error(`Failed to fetch trace (${res.status})`);
  return TraceDetailSchema.parse(await res.json());
}

export async function fetchExplanation(identifier: string): Promise<string> {
  const res = await fetch(`${BASE}/api/traces/${encodeURIComponent(identifier)}/explanation`);
  if (!res.ok) throw new Error(`Failed to fetch explanation (${res.status})`);
  return ExplanationResponseSchema.parse(await res.json()).explanation;
}
