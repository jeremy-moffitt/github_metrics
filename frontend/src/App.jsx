import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || window.location.origin;

async function getJson(url, signal) {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    signal,
  });
  const body = await response.json();

  if (!response.ok) {
    throw new Error(body.error || `Request failed (${response.status}).`);
  }

  return body;
}

function formatDuration(seconds) {
  if (seconds == null) return "No reviews";
  if (seconds < 60) return `${Math.round(seconds)} sec`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(0)} min`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} hr`;
  return `${(seconds / 86400).toFixed(1)} days`;
}

function ReviewTimeTick({ x, y, payload }) {
  const hours = Number(payload.value);
  const label =
    hours < 1 ? `${Math.round(hours * 60)}m` : `${Number(hours.toFixed(1))}h`;
  return (
    <text x={x} y={y} dy={4} textAnchor="middle" className="axis-tick">
      {label}
    </text>
  );
}

function MetricsTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;

  const values = Object.fromEntries(payload.map((item) => [item.dataKey, item.value]));
  return (
    <div className="chart-tooltip">
      <strong>{label}</strong>
      <span className="tooltip-opened">
        Opened: {values.opened ?? 0}
      </span>
      <span className="tooltip-closed">
        Closed: {values.closed ?? 0}
      </span>
      <span className="tooltip-review">
        Avg. first review:{" "}
        {values.averageReviewHours == null
          ? "No reviews"
          : formatDuration(values.averageReviewHours * 3600)}
      </span>
    </div>
  );
}

function RepositoryChart({ repository, metrics, cached, error }) {
  const chartData = metrics?.map((metric) => ({
    ...metric,
    averageReviewHours:
      metric.average == null ? null : metric.average / 3600,
  }));
  const averageOpened = metrics?.length
    ? metrics.reduce((sum, metric) => sum + metric.opened, 0) / metrics.length
    : null;

  return (
    <section className="repository-card" aria-labelledby={`title-${repository.name}`}>
      <header className="card-header">
        <div>
          <p className="eyebrow">Repository</p>
          <h2 id={`title-${repository.name}`}>{repository.name}</h2>
        </div>
        {cached && <span className="cache-badge">Cached</span>}
      </header>

      {error ? (
        <div className="repository-error" role="alert">
          <strong>Could not load metrics</strong>
          <p>{error}</p>
        </div>
      ) : !metrics ? (
        <div className="chart-loading" aria-label="Loading repository metrics">
          <span className="spinner" />
          <span>Loading monthly metrics…</span>
        </div>
      ) : (
        <>
          <div className="chart-summary">
            <span>
              <strong>
                {averageOpened == null ? "—" : averageOpened.toFixed(1)}
              </strong>
              <small>average PRs opened / month</small>
            </span>
            <span>
              <strong>{metrics.length}</strong>
              <small>months reported</small>
            </span>
          </div>
          <div
            className="chart"
            role="img"
            aria-label={`Monthly opened and closed pull requests and average first review time for ${repository.name}`}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={chartData}
                margin={{ top: 12, right: 12, bottom: 4, left: -12 }}
                barGap={5}
              >
                <CartesianGrid stroke="#e8edf5" vertical={false} />
                <XAxis
                  dataKey="month"
                  tickLine={false}
                  axisLine={false}
                  tick={{ fill: "#718096", fontSize: 12 }}
                  tickMargin={12}
                />
                <YAxis
                  yAxisId="prs"
                  allowDecimals={false}
                  width={38}
                  tickLine={false}
                  axisLine={false}
                  tick={{ fill: "#718096", fontSize: 12 }}
                  label={{
                    value: "PRs",
                    angle: -90,
                    position: "insideLeft",
                    fill: "#718096",
                    fontSize: 11,
                  }}
                />
                <YAxis
                  yAxisId="review"
                  orientation="right"
                  width={44}
                  tickLine={false}
                  axisLine={false}
                  tick={<ReviewTimeTick />}
                  label={{
                    value: "First review",
                    angle: 90,
                    position: "insideRight",
                    fill: "#718096",
                    fontSize: 11,
                  }}
                />
                <Tooltip content={<MetricsTooltip />} cursor={{ fill: "#f3f6fb" }} />
                <Legend
                  verticalAlign="top"
                  align="left"
                  iconType="circle"
                  iconSize={8}
                  wrapperStyle={{ paddingBottom: 16, fontSize: 12 }}
                />
                <Bar
                  yAxisId="prs"
                  dataKey="opened"
                  name="Opened"
                  fill="#5478e8"
                  radius={[4, 4, 0, 0]}
                  maxBarSize={24}
                />
                <Bar
                  yAxisId="prs"
                  dataKey="closed"
                  name="Closed"
                  fill="#38a889"
                  radius={[4, 4, 0, 0]}
                  maxBarSize={24}
                />
                <Bar
                  yAxisId="review"
                  dataKey="averageReviewHours"
                  name="Avg. first review"
                  fill="#e6a23c"
                  radius={[4, 4, 0, 0]}
                  maxBarSize={18}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="chart-note">
            Counts use the left axis. Average time to first submitted review
            uses the right axis. Months with no submitted reviews have no review
            time bar.
          </p>
        </>
      )}
    </section>
  );
}

export default function App() {
  const [repositories, setRepositories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    const controller = new AbortController();

    async function loadDashboard() {
      try {
        const directory = await getJson(
          new URL("/api/repositories", API_BASE_URL),
          controller.signal,
        );
        if (!Array.isArray(directory.repositories)) {
          throw new Error("The API returned an invalid repository list.");
        }

        const initialRepositories = directory.repositories.map((repository) => ({
          ...repository,
          metrics: null,
          cached: false,
          error: "",
        }));
        setRepositories(initialRepositories);
        setLoading(false);

        await Promise.all(
          initialRepositories.map(async (repository) => {
            try {
              const metricsUrl = new URL(repository.metrics_url, API_BASE_URL);
              const result = await getJson(metricsUrl, controller.signal);
              if (!Array.isArray(result.metrics)) {
                throw new Error("The API returned invalid metrics data.");
              }
              setRepositories((current) =>
                current.map((item) =>
                  item.name === repository.name
                    ? {
                        ...item,
                        metrics: result.metrics,
                        cached: Boolean(result.cached),
                      }
                    : item,
                ),
              );
            } catch (error) {
              if (error.name === "AbortError") return;
              setRepositories((current) =>
                current.map((item) =>
                  item.name === repository.name
                    ? { ...item, error: error.message }
                    : item,
                ),
              );
            }
          }),
        );
      } catch (error) {
        if (error.name !== "AbortError") {
          setLoadError(error.message);
          setLoading(false);
        }
      }
    }

    loadDashboard();
    return () => controller.abort();
  }, []);

  const readyCount = repositories.filter(
    (repository) => repository.metrics !== null,
  ).length;

  return (
    <main className="page-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />
      <div className="content">
        <header className="page-header">
          <div className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path
                d="M4 19V10m8 9V5m8 14v-7"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
              <path
                d="M2 19h20"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
          </div>
          <div>
            <p className="eyebrow">Engineering insights</p>
            <h1>GitHub PR metrics</h1>
            <p className="page-description">
              Pull request activity and first-review times, month by month.
            </p>
          </div>
          {!loading && !loadError && (
            <div className="status-pill">
              <span className="status-dot" />
              {readyCount} of {repositories.length} repositories loaded
            </div>
          )}
        </header>

        {loading && (
          <div className="page-state" role="status">
            <span className="spinner" />
            <span>Connecting to the metrics server…</span>
          </div>
        )}
        {loadError && (
          <div className="page-error" role="alert">
            <strong>Could not connect to the metrics server</strong>
            <p>{loadError}</p>
            <span>
              Start the Python server and check that the dashboard API URL is
              configured correctly.
            </span>
          </div>
        )}
        {!loading && !loadError && repositories.length === 0 && (
          <div className="page-state">
            No repositories are configured on the metrics server.
          </div>
        )}
        {repositories.length > 0 && (
          <div className="repository-grid">
            {repositories.map((repository) => (
              <RepositoryChart
                key={repository.name}
                repository={repository}
                metrics={repository.metrics}
                cached={repository.cached}
                error={repository.error}
              />
            ))}
          </div>
        )}

        <footer className="page-footer">
          <span>Metrics grouped by pull request opening month · UTC</span>
          <span>
            Refreshed on page load
            {repositories.some((repository) => repository.cached) &&
              " · some results served from cache"}
          </span>
        </footer>
      </div>
    </main>
  );
}
