# Uptime Monitor

A self-hosted uptime monitor for people who ship side projects and want to know when one of them goes down — without paying a monthly SaaS bill or running a server they have to babysit.

It runs on Azure Functions, checks your URLs on a schedule, keeps a durable history of every check, and pings you on Discord the moment something breaks. The whole thing costs effectively nothing to run on a consumption plan and is reproducible from scratch with one `terraform apply`.

---

## 1. Overview

**What it is.** A small, focused service that periodically requests a list of URLs, records whether each one responded healthily, stores that history, and exposes it through a JSON API and a couple of plain HTML pages. When a site fails, it sends an alert.

**Who it's for.** Solo developers and small teams who run several deployed apps (think a few Vercel/Render/Azure front-ends and APIs) and need a single place that answers "is everything up right now, and has anything been flaky lately?"

**The problem it solves.** When you have three or four side projects live, you usually find out one is down because *a user tells you* — or you never find out at all. This gives you a cheap, always-on watcher you fully control, with a history you can actually query.

---

## 2. Why This Exists

Hosted monitoring tools (UptimeRobot, Pingdom, Better Uptime, etc.) are genuinely good. But for a developer running personal projects they come with friction that doesn't fit the use case:

- **They're priced for businesses.** Free tiers cap the number of monitors or the check frequency, and the useful features (history retention, alert routing) sit behind a subscription that's hard to justify for hobby apps.
- **Your data lives in their account.** History, thresholds, and config are locked in someone else's dashboard. If you want to query "what was Kairos's uptime last week" programmatically, you're stuck with their UI.
- **They're a black box.** You can't change how "healthy" is defined, where alerts go, or how results are stored without whatever knobs the vendor exposes.

The goal here isn't to out-feature those products. It's to own the whole loop — checks, storage, alerts, and infrastructure — in a codebase small enough to read in one sitting and cheap enough to forget about on the bill.

---

## 3. Core Idea

Most of the design comes from one decision: **the act of checking a URL is separated from every way of consuming the result.**

A single function, `run_health_checks()`, does the work — fetch each URL, decide healthy/unhealthy, persist the result, fire alerts. Everything else is just a different *trigger* or *view* on top of that:

- The **timer** calls it every hour.
- The **HTTP endpoints** call it on demand if the cache is cold, then read from it.
- The **dashboard** and **history views** are presentation layers over the same stored data.

That separation is why adding a new alert channel or a new view doesn't touch the checking logic, and why the timer and the API can never drift out of sync — there's only one implementation of "what does a check mean."

---

## 4. Key Features

- **Scheduled checks.** A timer trigger runs every hour (NCRONTAB `0 0 * * * *`) and checks every URL in your config. No cron server, no always-on VM.
- **On-demand checks.** Hitting `/api/health-report` or `/api/dashboard` after a cold start runs the checks live, so you always get a real answer instead of an empty cache.
- **Durable history.** Every single check is written to Azure Table Storage, partitioned by app name, so history survives restarts and scales cheaply.
- **Queryable history API.** `GET /api/history` returns recent checks as JSON, filterable by app (`?app=Kairos`) and limit (`?limit=10`), newest first.
- **Human dashboard.** `GET /api/dashboard` renders current status with green/red badges plus rolling stats from the last 10 checks per app: uptime %, average response time, failure count, and the most recent error.
- **Discord alerts.** When a site is unhealthy, you get a Discord message with the app, URL, status code, response time, timestamp, and error — at most one per app per run, so a flapping site can't spam you.
- **Graceful degradation.** If history storage is unreachable, the dashboard still shows live status and displays a clear warning instead of erroring out.
- **Reproducible infrastructure.** A Terraform stack stands up a complete, isolated environment (resource group, storage, table, Function App, Application Insights) without touching anything that already exists.

---

## 5. Architecture & Tech Stack

```text
                MONITORED_URLS (config)
                        |
        timer (hourly) ─┤   ┌─ HTTP request (on demand)
                        ▼   ▼
              ┌─────────────────────────┐
              │   run_health_checks()   │   the one source of truth
              │  • GET each URL          │
              │  • classify healthy?     │
              │  • persist each result   │
              │  • alert on failures     │
              └─────────────────────────┘
                  │          │          │
        in-memory │   Azure  │   Discord│
        snapshot  ▼   Table   ▼  webhook ▼
     LATEST_RESULTS   Storage      (optional)
                  │   (history)
                  ▼
   ┌────────────┬───────────┬───────────┬──────────────┐
   │ /health-   │ /dashboard│ /history  │ /history-view │
   │  report    │  (HTML)   │  (JSON)   │  (HTML table) │
   └────────────┴───────────┴───────────┴──────────────┘
```

| Technology | Why this one |
|------------|--------------|
| **Azure Functions (Python, v2 model)** | The workload is bursty and tiny — a few HTTP requests once an hour. A consumption-based function scales to zero and costs almost nothing, which matches the "watch my side projects" use case far better than a VM or container that runs 24/7. The v2 decorator model keeps all triggers in one file with no `function.json` sprawl. |
| **Timer trigger** | Scheduling is a first-class platform feature, so there's no separate scheduler, cron daemon, or "keep this process alive" problem. The platform guarantees the hourly run. |
| **Azure Table Storage** | History is append-only, semi-structured, and read with simple key lookups — exactly what a NoSQL key/value store is good at. It's dramatically cheaper than a relational DB for this access pattern, needs no schema migration, and reuses the storage account the Functions runtime already requires. Partitioning by app name makes per-app history a cheap query. |
| **`requests`** | The checking logic is just HTTP with a timeout. A small, battle-tested client beats pulling in an async framework for an hourly job. |
| **Application Insights** | Functions emits Python `logging` output straight into it, so I get searchable logs and failure traces without standing up my own logging stack. |
| **Discord webhooks** | Alerting needed a channel with zero infrastructure. A Discord incoming webhook is one `POST` — no SMTP server, no third-party email API key, no inbox to manage — which is why it's the default channel. |
| **Terraform** | The environment is worth describing as code so it's reproducible and disposable. Terraform's `plan`/`apply`/`destroy` loop makes it safe to tear the whole thing down and rebuild it, which is the point of a sandbox env. |
| **GitHub Actions** | Code changes ship on push to `main` via a publish profile. It keeps deployment boring and removes the "did I remember to publish?" step. |

**Two storage layers, on purpose.** `LATEST_RESULTS` is an in-memory snapshot for instant reads; Table Storage is the durable record. The memory layer is a cache, not the source of truth — losing it on a cold start costs nothing because history is safe in storage.

---

## 6. How It Works

**From the operator's point of view:**

1. You list the apps you care about in `MONITORED_URLS` — a JSON array of `{ "name", "url" }`.
2. Every hour, the timer wakes the function and runs the checks. You don't do anything.
3. If something is down, Discord pings you with the details.
4. Whenever you're curious, you open `/api/dashboard` to see current status and recent reliability, or call `/api/history` to pull the raw data.

**What happens to a single check, end to end:**

1. `run_health_checks()` reads and parses `MONITORED_URLS`.
2. For each app, `_check_single_url()` issues a `GET` with a 10-second timeout and times the response.
3. The result is classified healthy if the status is 2xx or 3xx; a network error or timeout becomes an unhealthy result with the exception message instead of a crash.
4. Each result is written to Table Storage with `PartitionKey = app name` and `RowKey = millisecond-timestamp + short UUID` — the timestamp keeps rows naturally ordered, the UUID guarantees uniqueness.
5. After all checks, `_send_discord_alerts_for_unhealthy()` walks the results, skips healthy ones, and sends a single alert per failed app (tracked with a `set` so duplicates in the same run are suppressed).
6. The full result list is cached in `LATEST_RESULTS` and returned.

**When you read it back:**

- `/api/history` calls `fetch_history()`, which queries Table Storage (a partition filter when `?app=` is supplied), sorts newest-first, and trims to the limit.
- `/api/dashboard` pulls the last 10 checks per app, runs `_compute_app_stats()` (uptime %, average response time, failures, latest error), and renders badges. If that storage read throws, it catches it, logs a warning, and falls back to showing just the live snapshot with a banner.

---

## 7. Key Technical Decisions

**One checking function, many triggers.** The timer and every HTTP endpoint funnel through `run_health_checks()`. This is the decision the rest of the design hangs on: behavior can't diverge between "the scheduled check" and "the check I triggered by loading the dashboard," because they're literally the same code path.

**Cache vs. source of truth, made explicit.** `LATEST_RESULTS` exists purely for speed and for surviving the gap before the first timer run. It is allowed to be empty or stale; the durable answer always comes from Table Storage. Treating the in-memory layer as disposable is what makes cold starts a non-issue rather than a bug.

**Failures are data, not exceptions.** A site being down is a normal, expected outcome — so a timeout or connection error is caught and turned into an unhealthy *result* with its error text recorded. The function completes successfully and the failure shows up in history and alerts. Exceptions are reserved for genuinely broken states (e.g. storage misconfiguration), which return structured JSON errors with hints.

**Alerting is isolated behind one orchestrator.** `_send_discord_alerts_for_unhealthy()` is the only place that decides *who* gets alerted and *how often*. The actual send (`_send_discord_alert()`) is a leaf function. Adding Slack or email means adding a sibling send-helper and calling it from the orchestrator — the checking logic never learns about new channels.

**Config over code.** Everything environment-specific — the URL list, table name, webhook, storage connection — comes from environment variables. The same build runs locally against Azurite and in Azure against real storage with no code changes. This is also what keeps secrets out of the repo.

**Infrastructure is a separate, disposable environment.** The Terraform stack deliberately provisions a *new* resource group and Function App rather than importing the existing hand-made one. The live app stays untouched, and the IaC environment can be destroyed and rebuilt freely.

---

## 8. Challenges & Solutions

**Cold starts returned empty data.** On a consumption plan the app spins down, so the in-memory cache is empty on the first request after idle. Early on, `/api/health-report` could return nothing. **Fix:** endpoints detect an empty cache and run the checks on demand, so the first caller pays a small latency cost but always gets a real result.

**Local development needs real storage.** The timer trigger and Table Storage both expect a storage account, which doesn't exist on a laptop. Running locally failed against `127.0.0.1:10000`. **Fix:** use **Azurite**, the official local Azure Storage emulator, with `AzureWebJobsStorage=UseDevelopmentStorage=true`. Documented as a required step so local runs match production behavior.

**Deploying to Flex Consumption broke the usual deploy.** A standard zip deploy returned 404s because the live app runs on Flex Consumption, which expects a different packaging and a remote build. **Fix:** the GitHub Actions workflow installs dependencies into `.python_packages/lib/site-packages` and sets `sku: flexconsumption` with `remote-build: true`.

**A flapping site could spam alerts.** A site that fails several checks could, in a naive design, fire repeated messages. **Fix:** the alert orchestrator tracks already-alerted apps in a `set` within a run, so each unhealthy app produces at most one message per execution.

**Portal couldn't browse the tables.** On a student account, the Azure Portal's table browser hit a permissions wall, making it look like nothing was being stored. **Fix:** the data was fine — added `/api/history` (JSON) and `/api/history-view` (HTML) so history is always inspectable through the app itself, independent of portal permissions.

---

## 9. Limitations

Being honest about where this stops being clever:

- **History reads scan, then sort.** `fetch_history()` pulls matching rows into memory and sorts before trimming. That's fine at hobby scale (hundreds to low thousands of rows) but wouldn't hold up at millions — it would need time-bounded partition queries or server-side paging.
- **In-memory cache is per-instance.** `LATEST_RESULTS` lives in one worker. Under scale-out, different instances have different snapshots. The durable history is consistent; the fast-path snapshot isn't shared.
- **Alert dedupe is per-run only.** Within a single execution a site gets one alert, but there's no memory across runs — a site down for hours still alerts once per hour. There's no "recovered" notification yet either.
- **Endpoints are public.** The HTTP routes are anonymous. Fine for non-sensitive status data, but anyone with the URL can read it. No auth layer yet.
- **Discord is the only built-in channel.** Email and Slack are designed-for but not implemented.
- **Fixed hourly cadence.** The schedule is one interval for all apps; you can't yet check a critical API every 5 minutes and a blog daily.

---

## 10. Future Improvements

Realistic next steps, roughly in priority order:

- **More alert channels.** Slack is a near drop-in (incoming webhook, same shape as Discord). Email via Azure Communication Services or SendGrid is the next step for non-chat notifications. Both slot into the existing alert orchestrator.
- **Cross-run alert state.** Persist "currently alerting" status so a long outage doesn't re-notify every hour, and send a **recovery** message when a site comes back.
- **Per-app schedules and thresholds.** Let each app define its own interval and its own definition of healthy (e.g. expected status code or max latency).
- **Authentication on endpoints.** Function keys or Entra ID for the read APIs and a protected admin view.
- **Smarter history queries.** Time-range filters and partition-scoped pagination so the history API stays fast as data grows.
- **Application Insights alert rules and a workbook** for trend dashboards beyond the in-app stats.

---

## Tech reference

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FUNCTIONS_WORKER_RUNTIME` | Yes | Must be `python` |
| `AzureWebJobsStorage` | Yes | Storage connection for the Functions runtime and Table Storage |
| `MONITORED_URLS` | Yes | JSON array of `{ "name", "url" }` objects |
| `UPTIME_TABLE_NAME` | No | History table name (default: `UptimeChecks`) |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook for unhealthy alerts (empty = alerts disabled) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | No | Enables logs/metrics in Application Insights (set by Terraform) |

`MONITORED_URLS` example:

```json
[
  {"name": "Kairos", "url": "https://kairos-six-inky.vercel.app/"},
  {"name": "MoneyCheck", "url": "https://money-check-theta.vercel.app/dashboard"},
  {"name": "CourtLedger", "url": "https://court-ledger.vercel.app/"}
]
```

### API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health-report` | Latest check results as JSON |
| `GET /api/dashboard` | HTML dashboard: current status + last-10 stats per app |
| `GET /api/history` | Stored history as JSON (default limit 50, newest first) |
| `GET /api/history?app=Kairos` | Filter history by app name |
| `GET /api/history?limit=10` | Limit number of records |
| `GET /api/history-view` | Stored history as an HTML table |

### Project structure

```text
.
├── function_app.py              # all functions: timer + HTTP, and the shared helpers
├── host.json
├── requirements.txt
├── local.settings.json.example  # template for local config (real file is gitignored)
├── .github/workflows/deploy.yml # CI/CD to the live app
├── terraform/                   # reproducible, isolated Azure environment (IaC)
└── PROJECT_EXPLAINED.md         # deep-dive walkthrough of the codebase
```

### Run it locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy local.settings.json.example local.settings.json   # then edit MONITORED_URLS

# Local storage emulator (separate terminal), required for the timer + Table Storage
npm install -g azurite
azurite

func start
```

Then open `http://127.0.0.1:7071/api/dashboard`.

### Deploy

Push to `main` (GitHub Actions handles it), or deploy manually:

```powershell
az login
func azure functionapp publish <function-app-name> --python
```

Provision a fresh environment with Terraform: see [`terraform/README.md`](terraform/README.md).

### Discord alerts

Create a webhook (Discord → Server Settings → Integrations → Webhooks), then set `DISCORD_WEBHOOK_URL` in your app settings. Leave it empty to disable alerts entirely — the code skips the send silently.

### Logs

Python `logging` flows into Azure. View it in the Portal under your Function App → **Log stream** (live) or **Application Insights → Logs** (searchable history). The app logs check start/finish, unhealthy results, storage save outcomes, and alert outcomes.

### Troubleshooting

- **Empty `/api/health-report`** — check `MONITORED_URLS` is valid JSON with `name` and `url`.
- **`/api/history` errors locally** — start Azurite before `func start`, then hit `/api/health-report` once to create rows.
- **Dashboard shows a storage warning** — history storage is unreachable; live status still renders.
- **Discord silent** — confirm `DISCORD_WEBHOOK_URL` is set and check logs for send errors.
- **Can't browse tables in the Portal** — common on student accounts; use `/api/history` or `/api/history-view` instead.

---

## License

MIT.
