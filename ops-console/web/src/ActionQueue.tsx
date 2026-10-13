import { useEffect, useState } from "react";

type AgentAction = {
  action_id: string;
  action_type: string;
  status: string;
  created_at: string;
  scope?: string;
  attempt_count?: number;
  authority_version?: number;
  policy_version?: number;
  subject?: {
    probe?: boolean;
  };
  effect?: {
    ticket_id?: string;
    status?: string;
    short_description?: string;
  };
};

type ActionQueueProps = {
  refreshVersion: number;
  onRefresh: () => void;
};

export default function ActionQueue({
  refreshVersion,
  onRefresh,
}: ActionQueueProps) {
  const [actions, setActions] = useState<AgentAction[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    async function loadActions() {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch("/api/actions", {
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error(`Could not load actions (${response.status})`);
        }

        const data: { actions: AgentAction[] } = await response.json();
        setActions(data.actions);
      } catch (error) {
        if (controller.signal.aborted) return;

        setError(
          error instanceof Error ? error.message : "Could not load actions",
        );
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    void loadActions();
    return () => controller.abort();
  }, [refreshVersion]);

  const selectedAction = actions.find(
    (action) => action.action_id === selectedId,
  );

  return (
    <section className="action-queue">
      <div className="risk-toolbar">
        <h2>Action queue</h2>
        <button
            className="refresh-button"
            type="button"
            disabled={loading}
            onClick={onRefresh}
        >
            {loading ? "Loading…" : "Refresh actions"}
        </button>
       </div>

      {loading && <p role="status">Loading actions…</p>}
      {error && <p role="alert">{error}</p>}
      {error && actions.length > 0 && (
        <p>Showing the last successfully fetched actions.</p>
      )}
      {!loading && !error && actions.length === 0 && (
        <p>No actions found.</p>
      )}

      <div className="action-layout">
        <ul>
          {actions.map((action) => (
            <li key={action.action_id}>
              <button
                type="button"
                aria-pressed={selectedId === action.action_id}
                onClick={() => setSelectedId(action.action_id)}
              >
                <strong>{action.action_type}</strong>
                <br />
                {action.status}
                {action.subject?.probe && " · Probe"}
                <br />
                {new Date(action.created_at).toLocaleString()}
              </button>
            </li>
          ))}
        </ul>

        <div aria-label="Selected action details">
          {selectedAction ? (
            <>
              <h3>{selectedAction.effect?.short_description ?? "Action details"}</h3>
              <dl>
                <dt>Action ID</dt>
                <dd>{selectedAction.action_id}</dd>

                <dt>Scope</dt>
                <dd>{selectedAction.scope ?? "Not recorded"}</dd>

                <dt>Attempts</dt>
                <dd>{selectedAction.attempt_count ?? "Not recorded"}</dd>

                <dt>Authority version</dt>
                <dd>{selectedAction.authority_version ?? "Not recorded"}</dd>

                <dt>Policy version</dt>
                <dd>{selectedAction.policy_version ?? "Not recorded"}</dd>

                <dt>Recorded ticket ID</dt>
                <dd>{selectedAction.effect?.ticket_id ?? "Not recorded"}</dd>

                <dt>Effect status</dt>
                <dd>{selectedAction.effect?.status ?? "Not recorded"}</dd>
              </dl>
            </>
          ) : (
            <p>Select an action to inspect its execution details.</p>
          )}
        </div>
      </div>
    </section>
  );
}