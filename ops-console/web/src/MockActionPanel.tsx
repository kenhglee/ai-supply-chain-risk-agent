import { useState } from "react";

type MockActionResult = {
  action_id: string;
  outcome: "ok" | "denied" | "error";
  action_status?: string;
  authority_version?: number;
  policy_version?: number;
  denied_layer?: string;
  denial_status?: string;
  ticket_id?: string;
  error_message?: string;
};

type MockActionPanelProps = {
  onActionFinished: () => void;
};

export default function MockActionPanel({
  onActionFinished,
}: MockActionPanelProps) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<MockActionResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function runMockAction() {
    setRunning(true);
    setResult(null);
    setError(null);

    try {
      const response = await fetch("/api/actions/mock", {
        method: "POST",
      });

      if (!response.ok) {
        throw new Error(
          "Could not confirm the outcome. Refresh actions before retrying.",
        );
      }

      const data: { result: MockActionResult } = await response.json();
      setResult(data.result);
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Mock action request failed",
      );
    } finally {
      setRunning(false);
      onActionFinished();
    }
  }

  return (
    <section className="mock-action-panel">
      <h2>Governance test</h2>
      <p>
        Attempt a governed action that creates a mock ticket when admitted.
      </p>

      <button
        className="refresh-button"
        type="button"
        disabled={running}
        onClick={() => void runMockAction()}
      >
        {running ? "Running mock action…" : "Run mock action"}
      </button>

      {running && (
        <p role="status">Waiting for the executor result…</p>
      )}
      {error && <p role="alert">{error}</p>}

      {result && (
        <div role="status">
          <p>
            <strong>Outcome:</strong> {result.outcome}
          </p>
          <dl>
            <dt>Action ID</dt>
            <dd>{result.action_id}</dd>

            <dt>Action status</dt>
            <dd>{result.action_status ?? "Not recorded"}</dd>

            <dt>Authority version</dt>
            <dd>{result.authority_version ?? "Not recorded"}</dd>

            <dt>Policy version</dt>
            <dd>{result.policy_version ?? "Not recorded"}</dd>

            {result.ticket_id && (
              <>
                <dt>Mock ticket</dt>
                <dd>{result.ticket_id}</dd>
              </>
            )}

            {result.outcome === "denied" && (
              <>
                <dt>Denying layer</dt>
                <dd>{result.denied_layer ?? "Not recorded"}</dd>

                <dt>Denial status</dt>
                <dd>{result.denial_status ?? "Not recorded"}</dd>
              </>
            )}
          </dl>

          {result.outcome === "error" && (
            <p>{result.error_message ?? "Executor error"}</p>
          )}
        </div>
      )}
    </section>
  );
}