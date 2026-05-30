import { useEffect, useState } from 'react';
import './App.css';
import { fetchTraces, fetchTraceDetail, fetchExplanation } from './api';
import type { TraceSummary, TraceDetail, TraceStep } from './types';

function statusClass(status: string | null): string {
  if (status === 'ok') return 'badge badge-ok';
  if (status === 'inconclusive') return 'badge badge-inconclusive';
  return 'badge badge-unknown';
}

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString();
}

function slowestStep(steps: TraceStep[]): TraceStep | null {
  if (!steps.length) return null;
  return steps.reduce((a, b) => (b.duration_ms > a.duration_ms ? b : a));
}

// ---- TraceList ----

interface TraceListProps {
  traces: TraceSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

function TraceList({ traces, selectedId, onSelect }: TraceListProps) {
  if (!traces.length) return <p className="empty">No traces found.</p>;
  return (
    <ul className="trace-list">
      {traces.map((t) => {
        const id = t.trace_id ?? t.alert_id ?? '';
        return (
          <li
            key={id}
            className={`trace-item${id === selectedId ? ' selected' : ''}`}
            onClick={() => onSelect(id)}
          >
            <div className="trace-item-top">
              <span className="alert-id">{t.alert_id ?? '—'}</span>
              <span className={statusClass(t.final_status)}>{t.final_status ?? 'unknown'}</span>
            </div>
            <div className="trace-item-meta">
              <span>{t.run_duration_ms != null ? `${t.run_duration_ms}ms` : '—'}</span>
              <span>{formatDate(t.created_at)}</span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

// ---- TraceDetailPanel ----

interface TraceDetailPanelProps {
  detail: TraceDetail;
  explanation: string | null;
  explanationLoading: boolean;
}

function TraceDetailPanel({ detail, explanation, explanationLoading }: TraceDetailPanelProps) {
  const steps = detail.trace_steps ?? [];
  const slowest = slowestStep(steps);
  const decisions = steps.filter((s) => s.decision != null);
  const errors = steps.filter((s) => s.error != null);

  return (
    <div className="detail-panel">
      <h2 className="detail-headline">{detail.headline ?? '—'}</h2>

      <dl className="detail-meta">
        <dt>trace_id</dt>
        <dd className="mono">{detail.trace_id ?? '—'}</dd>

        <dt>alert_id</dt>
        <dd>{detail.alert_id ?? '—'}</dd>

        <dt>status</dt>
        <dd><span className={statusClass(detail.final_status)}>{detail.final_status ?? 'unknown'}</span></dd>

        <dt>supplier</dt>
        <dd>{detail.supplier ?? '—'}</dd>

        <dt>risk type</dt>
        <dd>{detail.risk_type ?? '—'}</dd>

        <dt>risk level</dt>
        <dd>{detail.risk_level ?? '—'}</dd>

        <dt>change</dt>
        <dd>{detail.change_type ?? '—'}</dd>

        <dt>run time</dt>
        <dd>{detail.run_duration_ms != null ? `${detail.run_duration_ms}ms` : '—'}</dd>

        <dt>created</dt>
        <dd>{formatDate(detail.created_at)}</dd>
      </dl>

      {steps.length > 0 && (
        <section className="detail-section">
          <h3>Node sequence</h3>
          <div className="node-sequence">
            {steps.map((s, i) => (
              <span key={i} className={`node${s === slowest ? ' node-slowest' : ''}`}>
                {s.node_name}
                <span className="node-ms">{s.duration_ms}ms</span>
                {i < steps.length - 1 && <span className="node-arrow">→</span>}
              </span>
            ))}
          </div>
          {slowest && (
            <p className="slowest-note">
              Slowest: <strong>{slowest.node_name}</strong> ({slowest.duration_ms}ms)
            </p>
          )}
        </section>
      )}

      {decisions.length > 0 && (
        <section className="detail-section">
          <h3>Decisions</h3>
          <table className="decision-table">
            <tbody>
              {decisions.map((s, i) => (
                <tr key={i}>
                  <td className="mono">{s.node_name}</td>
                  <td className="decision-arrow">→</td>
                  <td className="mono">{s.decision}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {errors.length > 0 && (
        <section className="detail-section">
          <h3>Errors</h3>
          <table className="decision-table error-table">
            <tbody>
              {errors.map((s, i) => (
                <tr key={i}>
                  <td className="mono">{s.node_name}</td>
                  <td className="decision-arrow">→</td>
                  <td className="mono">{s.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="detail-section">
        <h3>Explanation</h3>
        {explanationLoading
          ? <p className="loading-inline">Loading…</p>
          : <pre className="explanation">{explanation ?? '—'}</pre>
        }
      </section>
    </div>
  );
}

// ---- App ----

export default function App() {
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [explanation, setExplanation] = useState<string | null>(null);
  const [explanationLoading, setExplanationLoading] = useState(false);

  useEffect(() => {
    fetchTraces()
      .then(setTraces)
      .catch((e: Error) => setListError(e.message))
      .finally(() => setListLoading(false));
  }, []);

  function handleSelect(id: string) {
    setSelectedId(id);
    setDetail(null);
    setExplanation(null);
    setDetailError(null);

    setDetailLoading(true);
    fetchTraceDetail(id)
      .then(setDetail)
      .catch((e: Error) => setDetailError(e.message))
      .finally(() => setDetailLoading(false));

    setExplanationLoading(true);
    fetchExplanation(id)
      .then(setExplanation)
      .catch(() => setExplanation(null))
      .finally(() => setExplanationLoading(false));
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Risk Trace Viewer</h1>
      </header>

      <main className="app-body">
        <aside className="pane pane-list">
          <h2 className="pane-title">Traces</h2>
          {listLoading && <p className="loading">Loading traces…</p>}
          {listError && <p className="error">Error: {listError}</p>}
          {!listLoading && !listError && (
            <TraceList traces={traces} selectedId={selectedId} onSelect={handleSelect} />
          )}
        </aside>

        <section className="pane pane-detail">
          {!selectedId && <p className="empty">Select a trace to view details.</p>}
          {selectedId && detailLoading && <p className="loading">Loading detail…</p>}
          {selectedId && detailError && <p className="error">Error: {detailError}</p>}
          {selectedId && detail && !detailLoading && (
            <TraceDetailPanel
              detail={detail}
              explanation={explanation}
              explanationLoading={explanationLoading}
            />
          )}
        </section>
      </main>
    </div>
  );
}
