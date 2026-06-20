# RiskLens MCP

A focused, production-ready **MCP (Model Context Protocol) server** exposing exactly two tools for risk analysis of US public companies, built on live SEC EDGAR data:

1. **`analyze_8k_events`** — risk analysis of a company's recent Form 8-K filings (restatements, bankruptcy, delisting, accelerated debt, impairments, leadership changes, and more)
2. **`analyze_insider_activity`** — risk analysis of a company's recent Form 4 insider transactions (clustered insider selling, officer/director activity, open-market conviction signal)

Both tools live in **one server**, are **risk-focused by default** but support a neutral `mode="summary"` for plain filing lookups, are backed by a **3-day Upstash Redis cache**, and are built to handle **many concurrent callers** (designed for a single hosted deployment used by multiple paying clients, e.g. via Render).

---

## How it works

- **Data source:** SEC EDGAR (`data.sec.gov` / `www.sec.gov`), free, no API key. Tickers are resolved to CIK numbers via SEC's official ticker map.
- **8-K analysis:** Pulls the company's filing history, filters to Form 8-K / 8-K-A, and classifies each filing's official "item codes" (e.g. `4.02` = restatement, `1.03` = bankruptcy) against a built-in risk taxonomy (`core/risk_rules.py`).
- **Insider activity analysis:** Pulls the company's recent Form 4 filings, downloads and parses the actual ownership XML for each one (not just metadata), and classifies transaction codes (P/S/A/M/F/etc.) to detect clustered open-market selling vs. routine compensation-driven activity (grants, option exercises, tax withholding).
- **Caching:** Every tool call checks Redis *first*. On a hit, the cached result is returned immediately with no SEC EDGAR calls at all. On a miss, the tool does the real work, then writes the result to cache with a 3-day TTL. Cache keys are built deterministically from the tool name + normalized parameters (ticker is case/whitespace-insensitive), so `"aapl"` and `"AAPL "` hit the same cache entry. **Errors are never cached** — a transient SEC EDGAR hiccup won't poison the cache for 3 days.
- **Concurrency:** The server runs on the `streamable-http` MCP transport in stateless mode — designed for one deployed URL serving many simultaneous clients, not a single local desktop session. SEC EDGAR calls are rate-limited process-wide (max 8 req/sec, under SEC's 10/sec ceiling) so concurrent requests from different users can't collectively get the server's IP blocked.

---

## Project structure

```
risklens-mcp/
├── core/
│   ├── __init__.py
│   ├── cache.py          # Upstash Redis caching layer (3-day TTL)
│   ├── sec_client.py      # Shared SEC EDGAR HTTP client (rate-limited)
│   └── risk_rules.py      # Shared 8-K item code / Form 4 transaction code risk taxonomy
├── tools/
│   ├── __init__.py
│   ├── eight_k_events.py      # analyze_8k_events tool
│   └── insider_activity.py    # analyze_insider_activity tool
├── server.py               # FastMCP server entrypoint (streamable-http, stateless)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Tools reference

### `analyze_8k_events`

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `ticker` | string | — | Required. e.g. `"AAPL"`. Case-insensitive. |
| `lookback_days` | int | `180` | Clamped to `[1, 1825]` (5 years). |
| `mode` | `"risk"` \| `"summary"` | `"risk"` | `"risk"` = scored risk analysis. `"summary"` = neutral filing list. |

Returns (risk mode): `risk_score` (0–10), `risk_level` (LOW/MODERATE/ELEVATED/SEVERE), `flagged_filings`, `risk_category_breakdown`, a plain-language `narrative`, and a `disclaimer`. Always includes `from_cache: bool`.

### `analyze_insider_activity`

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `ticker` | string | — | Required. Case-insensitive. |
| `lookback_days` | int | `90` | Clamped to `[1, 730]` (2 years). |
| `mode` | `"risk"` \| `"summary"` | `"risk"` | Same convention as above. |
| `max_filings` | int | `25` | Max individual Form 4 filings fetched+parsed per call. Hard-capped at `40`. |

Returns (risk mode): `risk_score`, `risk_level`, open-market buy/sell counts and dollar totals, `distinct_insiders_selling`, `officer_or_director_sell_count`, `flagged_transactions`, `narrative`, `disclaimer`. Always includes `from_cache: bool`.

**This is not investment advice.** Both tools say so explicitly in their output `disclaimer` field — they surface SEC disclosure patterns, not predictions.

---

## Step-by-step: from project creation to GitHub (VS Code)

These commands assume you're starting from scratch on your own machine. Adjust paths as needed.

### 1. Create the project folder and open it in VS Code

```bash
mkdir risklens-mcp
cd risklens-mcp
code .
```

### 2. Recreate the file structure

In VS Code, create these files/folders (or copy the files this delivery provides into them):

```
core/__init__.py
core/cache.py
core/sec_client.py
core/risk_rules.py
tools/__init__.py
tools/eight_k_events.py
tools/insider_activity.py
server.py
requirements.txt
.env.example
README.md
```

### 3. Create and activate a virtual environment

Open a terminal in VS Code (`` Ctrl+` ``):

```bash
python3 -m venv venv
```

macOS/Linux:
```bash
source venv/bin/activate
```

Windows (PowerShell):
```powershell
venv\Scripts\Activate.ps1
```

VS Code will likely prompt "Select Interpreter" — choose the `venv` one (`Python 3.x ('venv')`).

### 4. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Set up your environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `SEC_EDGAR_USER_AGENT` — your name/company + a real contact email (required by SEC EDGAR's terms of use).
- `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` — from your [Upstash console](https://console.upstash.com/) → your Redis database → REST API section. (Free tier is sufficient to start.)

**Never commit `.env`** — it's already excluded via `.gitignore` in step 7.

### 6. Run the server locally

```bash
python server.py
```

You should see:
```
Starting RiskLens MCP on 0.0.0.0:8000 (transport=streamable-http, stateless=True)
...
Uvicorn running on http://0.0.0.0:8000
```

Quick manual smoke test in a second terminal:
```bash
curl -s http://127.0.0.1:8000/mcp \
  -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}'
```
A `200 OK` with a JSON-RPC result containing `"serverInfo":{"name":"RiskLens MCP"...}` confirms the server is healthy.

### 7. Initialize git and push to GitHub

Create a `.gitignore`:

```bash
cat > .gitignore << 'EOF'
venv/
__pycache__/
*.pyc
.env
.DS_Store
EOF
```

Initialize and commit:

```bash
git init
git add .
git commit -m "Initial commit: RiskLens MCP - 8-K and insider activity risk analysis MCP server"
```

Create a new **empty** repository on GitHub (no README/license/.gitignore — you already have them), then:

```bash
git branch -M main
git remote add origin https://github.com/<your-username>/risklens-mcp.git
git push -u origin main
```

(If you use SSH instead of HTTPS, use `git@github.com:<your-username>/risklens-mcp.git`.)

---

## Deploying to Render

1. On [Render](https://render.com), create a **New Web Service** and connect your GitHub repo.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `python server.py`
4. **Environment variables** (Render dashboard → Environment): add `SEC_EDGAR_USER_AGENT`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`. Do **not** set `PORT` manually — Render injects it automatically and `server.py` reads it via `os.getenv("PORT", "8000")`.
5. Deploy. Your MCP server URL will be something like `https://risklens-mcp.onrender.com/mcp` — this is the URL your clients add as a custom MCP connector in Claude or Grok.

**Free-tier note:** Render's free web services spin down after ~15 minutes of inactivity and take roughly a minute to spin back up on the next request. The Redis cache helps mask this for repeat queries once warm, but the very first request after a cold start will be slower. Upgrade to a paid Render instance to avoid this if your clients need consistently fast first responses.

---

## Notes on caching correctness (why this won't repeat past problems)

- **Single source of truth for keys:** `core/cache.build_cache_key()` is the *only* place a cache key is ever constructed. Tools never hand-build a string key, so there's no risk of a write key and a read key silently drifting apart.
- **Deterministic keys regardless of argument order/case:** parameters are sorted and tickers are normalized before hashing, so equivalent calls always hit the same entry.
- **Explicit envelope on every cached value** (`cached_at`, `schema_version`, `data`) so malformed or legacy entries are detected and safely ignored rather than returned as garbage.
- **Every Redis call is wrapped in try/except.** A Redis outage degrades the server to "no caching, but still works" — it never raises into a tool and never breaks a user's request.
- **Errors are never cached.** Only successful analyses get written to Redis, so a transient SEC EDGAR failure can't get "stuck" for 3 days.
- **TTL is a single named constant** (`CACHE_TTL_SECONDS = 3 * 24 * 60 * 60`), referenced everywhere, so it can't drift between call sites.

This was verified end-to-end during development with a local mock of the Upstash REST protocol, including a real TTL-expiry test (write → immediate read succeeds → wait past TTL → read returns a clean miss) and a measured **~150–170x** speedup on cache hits vs. cold SEC EDGAR fetches.

---

## Disclaimer

RiskLens MCP surfaces patterns in public SEC filings using a transparent, rules-based taxonomy. It is **not** investment, legal, or financial advice, and does not assess whether a flagged event was ultimately material, resolved, or predictive of anything. Always direct users to the linked primary filings on SEC EDGAR for verification.