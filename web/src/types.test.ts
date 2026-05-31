import { describe, it, expect } from 'vitest';
import { TraceSummarySchema, TraceDetailSchema } from './types';

const validSummary = {
  alert_id: 'alert-123',
  trace_id: 'trace-456',
  headline: 'Supplier disruption reported near key fab',
  final_status: 'alert_raised',
  created_at: '2026-05-31T12:00:00Z',
  run_duration_ms: 1234,
};

const validStep = {
  node_name: 'analyze',
  started_at: '2026-05-31T12:00:00Z',
  ended_at: '2026-05-31T12:00:01Z',
  duration_ms: 950,
};

const validDetail = {
  ...validSummary,
  tool_decision: 'retrieve',
  supplier: 'TSMC',
  risk_type: 'geopolitical',
  risk_level: 'high',
  change_type: 'new_alert',
  trace_steps: [validStep],
};

describe('TraceSummarySchema', () => {
  it('parses a representative /api/traces array response', () => {
    const result = TraceSummarySchema.array().parse([validSummary]);
    expect(result).toHaveLength(1);
    expect(result[0].alert_id).toBe('alert-123');
    expect(result[0].run_duration_ms).toBe(1234);
  });

  it('accepts nullable fields as null', () => {
    const payload = { ...validSummary, alert_id: null, trace_id: null };
    expect(() => TraceSummarySchema.array().parse([payload])).not.toThrow();
  });

  it('throws when run_duration_ms is a string instead of a number', () => {
    const bad = { ...validSummary, run_duration_ms: '1234' };
    expect(() => TraceSummarySchema.array().parse([bad])).toThrow();
  });
});

describe('TraceDetailSchema', () => {
  it('parses a valid trace detail with trace_steps', () => {
    const result = TraceDetailSchema.parse(validDetail);
    expect(result.trace_steps).toHaveLength(1);
    expect(result.trace_steps[0].node_name).toBe('analyze');
  });

  it('throws when a trace_steps item is missing required duration_ms', () => {
    const { duration_ms: _omit, ...stepMissingField } = validStep;
    const bad = { ...validDetail, trace_steps: [stepMissingField] };
    expect(() => TraceDetailSchema.parse(bad)).toThrow();
  });

  it('throws when trace_steps is not an array', () => {
    const bad = { ...validDetail, trace_steps: null };
    expect(() => TraceDetailSchema.parse(bad)).toThrow();
  });
});
