import { useEffect, useState } from "react";

type Authority = {
  scope_id: string;
  status: string;
  version: number;
  updated_at: string;
  updated_by: string;
  reason: string;
};

export default function AuthorityPanel() {
  const [authority, setAuthority] = useState<Authority | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [reason, setReason] = useState("");
  const [revoking, setRevoking] = useState(false);
  const [revokeMessage, setRevokeMessage] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    async function loadAuthority() {
      setLoading(true);
      setError(null);

      try {
        const response = await fetch("/api/authority", {
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error(
            `Could not load authority (${response.status})`,
          );
        }

        const data: { authority: Authority } = await response.json();
        setAuthority(data.authority);
      } catch (error) {
        if (controller.signal.aborted) return;

        setError(
          error instanceof Error
            ? error.message
            : "Could not load authority",
        );
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    void loadAuthority();
    return () => controller.abort();
  }, [refreshVersion]);

  async function revokeAuthority() {
        setRevoking(true);
        setRevokeMessage(null);

        try {
            const response = await fetch("/api/authority/revoke", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ reason: reason.trim() }),
            });

            if (!response.ok) {
            throw new Error(
                "Could not confirm revocation. Refresh authority before retrying.",
            );
            }

            setRevokeMessage(
            "Revocation command completed. Fetching the authority record…",
            );
            setReason("");
            setRefreshVersion((version) => version + 1);
        } catch (error) {
            setRevokeMessage(
            error instanceof Error ? error.message : "Revocation request failed",
            );
        } finally {
            setRevoking(false);
        }
    }

  return (
    <section className="authority-panel">
      <div className="risk-toolbar">
        <h2>Agent authority</h2>
        <button
          className="refresh-button"
          type="button"
          disabled={loading}
          onClick={() => setRefreshVersion((version) => version + 1)}
        >
          {loading ? "Loading…" : "Refresh authority"}
        </button>
      </div>

      {loading && <p role="status">Reading authority…</p>}
      {error && <p role="alert">{error}</p>}

      {authority && (
        <>
          {(loading || error) && (
            <p>Showing the last successfully fetched record.</p>
          )}

          <dl>
            <dt>Recorded status</dt>
            <dd>{authority.status}</dd>

            <dt>Authority version</dt>
            <dd>{authority.version}</dd>

            <dt>Scope</dt>
            <dd>{authority.scope_id}</dd>

            <dt>Last updated</dt>
            <dd>{new Date(authority.updated_at).toLocaleString()}</dd>

            <dt>Updated by</dt>
            <dd>{authority.updated_by}</dd>

            <dt>Reason</dt>
            <dd>{authority.reason}</dd>
          </dl>
        </>
      )}

      <div className="authority-controls">
        <label htmlFor="revoke-reason">Revocation reason</label>
        <input
            id="revoke-reason"
            type="text"
            maxLength={500}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            disabled={revoking}
            placeholder="Why should agent authority be revoked?"
        />

        <button
            className="revoke-button"
            type="button"
            disabled={
            revoking ||
            loading ||
            error !== null ||
            authority?.status !== "ACTIVE" ||
            !reason.trim()
            }
            onClick={() => void revokeAuthority()}
        >
            {revoking ? "Revoking…" : "Revoke authority"}
        </button>

        {revokeMessage && <p role="status">{revokeMessage}</p>}
      </div>

      <p>
        This record describes agent authority. Capability policy and AWS
        permissions are enforced separately.
      </p>
    </section>
  );
}