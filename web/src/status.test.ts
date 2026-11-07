import { describe, it, expect } from 'vitest';
import { traceBadge } from './status';

describe('traceBadge', () => {
  it('shows completed alert statuses', () => {
    expect(traceBadge('ok', 'resumed_completed')).toEqual({ label: 'ok', className: 'badge badge-ok' });
    expect(traceBadge('inconclusive', null).className).toBe('badge badge-inconclusive');
    expect(traceBadge(null).label).toBe('unknown');
  });

  it('never shows a paused or denied attempt as a completed ok alert', () => {
    for (const event of ['paused', 'authority_denied', 'lease_lost', 'action_in_flight', 'attempt_failed', 'completion_unverified']) {
      const badge = traceBadge('ok', event);
      expect(badge.label).toBe(event);
      expect(badge.className).not.toContain('badge-ok');
    }
  });
});
