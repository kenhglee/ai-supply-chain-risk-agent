import { useEffect, useState } from "react";

type Risk = {
  pk: string;
  supplier: string;
  risk_type: string;
  current_risk_level: string;
  last_headline: string;
  last_seen_at: string;
};

export default function App() {
  const [risks, setRisks] = useState<Risk[]>([]);
  const [selectedRisk, setSelectedRisk] = useState<Risk | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();

    async function loadRisks() {

      setLoading(true);
      setError(null);

      try {
        const response = await fetch("/api/risks", {
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error(`Could not load risks (${response.status})`);
        }

        const data: { risks: Risk[]; count: number } =
          await response.json();

        setRisks(data.risks);
        setSelectedRisk((current) =>
          current
            ? data.risks.find((risk) => risk.pk === current.pk) ?? null
            : null,
        );
      } catch (error) {
        if (controller.signal.aborted) return;

        setError(
          error instanceof Error ? error.message : "Could not load risks",
        );
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    }

    void loadRisks();

    return () => controller.abort();
  }, [refreshVersion]);

  return (
    <main>
      <h1>Agent Operations Console</h1>
      <div className="risk-toolbar">
        <h2>Supplier risks</h2>
        <button
          className="refresh-button"
          type="button"
          disabled={loading}
          onClick={() => setRefreshVersion((version) => version + 1)}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {loading && <p role="status">Loading risks…</p>}
      {error && <p role="alert">{error}</p>}

      {!loading && !error && risks.length === 0 && (
        <p>No risk records found.</p>
      )}

      <ul>
        {risks.map((risk) => (
          <li key={risk.pk}>
            <button
              type="button"
              onClick={() => setSelectedRisk(risk)}
              aria-pressed={selectedRisk?.pk === risk.pk}
            >
              {risk.supplier} — {risk.risk_type} —{" "}
              {risk.current_risk_level}
            </button>
          </li>
        ))}
      </ul>

      {selectedRisk && (
        <section aria-label="Selected risk details">
          <h2>{selectedRisk.supplier}</h2>
          <p>
            <strong>Risk:</strong> {selectedRisk.risk_type}
          </p>
          <p>
            <strong>Level:</strong> {selectedRisk.current_risk_level}
          </p>
          <p>
            <strong>Latest headline:</strong> {selectedRisk.last_headline}
          </p>
          <p>
            <strong>Last observed:</strong>{" "}
            {new Date(selectedRisk.last_seen_at).toLocaleString()}
          </p>
        </section>
      )}
    </main>
  );
}