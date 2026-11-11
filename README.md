# GitHub pull request metrics

This script reports monthly pull request counts and the average and median time
from pull request creation to its first submitted review. It reports the current
month and the preceding months, in UTC.

## Configure

Copy the example configuration beside the script:

```powershell
Copy-Item .\github_pr_metrics.example.json .\github_pr_metrics.json
```

Edit `github_pr_metrics.json`:

- `repositories` is a non-empty list. Each entry must have a GitHub repository
  URL in `url`.
- `github_token` is the optional global token. Set it to a token string, or
  leave it `null` to make unauthenticated requests.
- `token` is optional for each repository. If set to a token string, it is used
  only for that repository. If omitted or `null`, that repository uses the
  global `github_token`.

The sample includes both token options. Replace the sample repository URLs and
placeholder values before running. Tokens need access to the repositories being
queried; unauthenticated requests are subject to GitHub's lower API rate limit.
Keep real tokens private and do not commit them to source control.

## Run

With Python 3.10 or later:

```powershell
py .\github_pr_metrics.py
```

By default, the script reads `github_pr_metrics.json` from the same directory as
the Python file and reports six months. Select another configuration file or
change the number of months with:

```powershell
py .\github_pr_metrics.py --config .\my-config.json --months 12
```

## Run the JSON API server

The server reads the same configuration and reuses the same metrics collector:

```powershell
py .\github_pr_metrics_server.py
```

By default it listens on `http://127.0.0.1:8000`, uses the same default config
file, and calculates six months of metrics. Its API endpoints are:

- `GET /api/repositories` returns the configured repository names and their
  metrics URLs. It does not expose tokens.
- `GET /api/repositories/{owner}/{repo}/metrics` returns JSON with the
  repository, month count, and metrics. Add `?months=12` to request a different
  range (1 through 36); if omitted, the server's `--months` default is used.
  The response also includes `cached`, indicating whether it was served from
  the server cache.

For example:

```powershell
py .\github_pr_metrics_server.py --config .\my-config.json --months 12
```

Successful metrics results are cached in memory for five minutes by default.
The cache is bounded to 128 repository/month combinations and evicts the
least-recently-used result when full. Adjust it with `--cache-ttl` (seconds) and
`--cache-size`; use `--cache-ttl 0` to disable caching. Failed GitHub requests
are not cached. Concurrent requests for the same repository and month range
share a single calculation.

To allow a React development server on another origin, set its exact origin:

```powershell
py .\github_pr_metrics_server.py --cors-origin http://localhost:5173
```

The API is unauthenticated and may return metrics for private repositories
configured on the server. Keep it bound to localhost unless access is otherwise
restricted. Do not expose it to an untrusted network.

## Run the React dashboard

The single-page dashboard is in `frontend/`. It loads the repository list when
opened, then requests and graphs metrics for every configured repository.
Started from the repository root, run the Python API server in one terminal:

```powershell
py .\github_pr_metrics_server.py
```

In another terminal, install the frontend dependencies and start Vite:

```powershell
Set-Location .\frontend
npm install
npm run dev
```

Open the local address printed by Vite (by default `http://localhost:5173`).
Vite proxies `/api` requests to `http://127.0.0.1:8000`, so CORS does not need
to be enabled for local development. If the Python API listens elsewhere, set
`VITE_API_PROXY_TARGET` to its origin before starting Vite, for example:

```powershell
$env:VITE_API_PROXY_TARGET = "http://127.0.0.1:8010"
npm run dev
```

Each repository has its own chart. Opened and closed PR counts use the left
axis, average time to first submitted review uses the right axis, and the card
also shows average PRs opened per month. Review durations without submitted
reviews are shown as missing rather than zero. For a separately hosted frontend,
set `VITE_API_BASE_URL` to the API server's origin at build time and configure
the server with `--cors-origin` for the frontend's exact origin.

The dashboard uses Node.js 20.19+ (or 22.12+) and npm. The API server remains
the source of repository configuration and GitHub credentials; the browser
never receives tokens.
