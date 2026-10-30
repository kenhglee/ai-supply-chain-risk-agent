// Checkpointed attempts that stopped before END (mirrors INCOMPLETE_WORKFLOW_EVENTS
// in app/storage/risk_trace_store.py). Their final_status is an intermediate
// analysis result and must not be shown as a completed outcome.
const INCOMPLETE_WORKFLOW_EVENTS = new Set(['paused', 'authority_denied']);

export interface TraceBadge {
  label: string;
  className: string;
}

export function traceBadge(finalStatus: string | null, workflowEvent?: string | null): TraceBadge {
  if (workflowEvent && INCOMPLETE_WORKFLOW_EVENTS.has(workflowEvent)) {
    return { label: workflowEvent, className: 'badge badge-unknown' };
  }
  if (finalStatus === 'ok') return { label: 'ok', className: 'badge badge-ok' };
  if (finalStatus === 'inconclusive') return { label: 'inconclusive', className: 'badge badge-inconclusive' };
  return { label: finalStatus ?? 'unknown', className: 'badge badge-unknown' };
}
