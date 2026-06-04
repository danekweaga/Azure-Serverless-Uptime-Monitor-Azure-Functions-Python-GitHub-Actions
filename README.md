# Azure Serverless Uptime Monitor

A beginner-friendly **serverless uptime monitor** built with **Python Azure Functions**. It checks URLs every hour, stores history in **Azure Table Storage**, exposes JSON/HTML APIs, and can send optional **Discord alerts**.

Good for interviews: timer trigger, HTTP APIs, durable storage, env-based config, logging, and GitHub Actions CI/CD.

## Architecture

```text
MONITORED_URLS
      |
      v
+---------------------------+
| scheduled_health_check    |  every hour
| + on-demand HTTP checks   |
+---------------------------+
      |
      +--> LATEST_RESULTS (in-memory latest snapshot)
      |
      +--> Azure Table Storage (UptimeChecks history)
      |
      +--> Discord webhook (optional, unhealthy only)
      |
      v
+-----------+  +-----------+  +-------------+  +--------------+
| health-   |  | dashboard |  | history     |  | history-view |
| report    |  | (HTML)    |  | (JSON)      |  | (HTML table) |
+-----------+  +-----------+  +-------------+  +--------------+
```

## Tech stack

- Python 3.11+ / Azure Functions v2 programming model
- Timer trigger + HTTP triggers
- `requests` for URL checks
- `azure-data-tables` for persistent history
- Azure Table Storage via `AzureWebJobsStorage`
- GitHub Actions deploy (Flex Consumption)

## Project structure

```text
.
├── function_app.py
├── host.json
├── requirements.txt
├── local.settings.json.example
├── .github/workflows/deploy.yml
└── README.md
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FUNCTIONS_WORKER_RUNTIME` | Yes | Must be `python` |
| `AzureWebJobsStorage` | Yes | Storage connection for Functions runtime and Table Storage |
| `MONITORED_URLS` | Yes | JSON array of `{ "name", "url" }` objects |
| `UPTIME_TABLE_NAME` | No | Table name for history (default: `UptimeChecks`) |
| `DISCORD_WEBHOOK_URL` | No | Discord webhook URL for unhealthy alerts (leave empty to disable) |

### `MONITORED_URLS` example

```json
[
  {"name": "Kairos", "url": "https://kairos-six-inky.vercel.app/"},
  {"name": "MoneyCheck", "url": "https://money-check-theta.vercel.app/dashboard"},
  {"name": "CourtLedger", "url": "https://court-ledger.vercel.app/"}
]
```

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health-report` | Latest check results as JSON |
| `GET /api/dashboard` | HTML dashboard with latest status + history stats |
| `GET /api/history` | Stored history as JSON (default limit 50) |
| `GET /api/history?app=Kairos` | Filter history by app name |
| `GET /api/history?limit=10` | Limit number of records |
| `GET /api/history-view` | Stored history as an HTML table |

## Local setup

### 1. Clone and create a virtual environment

```powershell
git clone <your-repo-url>
cd Azure-Serverless-Uptime-Monitor-Azure-Functions-Python-GitHub-Actions

py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure local settings

```powershell
copy local.settings.json.example local.settings.json
```

Edit `local.settings.json`:

- Set `MONITORED_URLS`
- Optional: `UPTIME_TABLE_NAME`, `DISCORD_WEBHOOK_URL`

### 3. Run Azurite (local storage)

Table Storage and the timer need storage locally:

```powershell
npm install -g azurite
azurite
```

Keep Azurite running in a second terminal.

### 4. Start the Function App

```powershell
func start
```

### 5. Test locally

```text
http://127.0.0.1:7071/api/health-report
http://127.0.0.1:7071/api/dashboard
http://127.0.0.1:7071/api/history
http://127.0.0.1:7071/api/history?app=Kairos&limit=10
http://127.0.0.1:7071/api/history-view
```

## Azure deployment

### Manual deploy

```powershell
az login
func azure functionapp publish func-uptime-monitor-dan --python
```

### Set Azure app settings

```powershell
az functionapp config appsettings set `
  --name func-uptime-monitor-dan `
  --resource-group rg-uptime-monitor `
  --settings `
    MONITORED_URLS='[{"name":"Kairos","url":"https://kairos-six-inky.vercel.app/"}]' `
    UPTIME_TABLE_NAME=UptimeChecks `
    DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/your-webhook-id/your-token'
```

Or use **Azure Portal** -> Function App -> **Environment variables**.

## GitHub Actions deployment

Workflow: [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)

Required:

- Secret: `AZURE_FUNCTIONAPP_PUBLISH_PROFILE`
- Variable: `AZURE_FUNCTIONAPP_NAME`

Flex Consumption settings in workflow:

- `sku: flexconsumption`
- `remote-build: true`

Push to `main` to deploy. Configure `MONITORED_URLS`, `UPTIME_TABLE_NAME`, and `DISCORD_WEBHOOK_URL` in Azure after deploy.

## Discord alerts

1. In Discord: Server Settings -> Integrations -> Webhooks -> New Webhook
2. Copy the webhook URL
3. Set `DISCORD_WEBHOOK_URL` in Azure app settings (or `local.settings.json` locally)
4. When a check returns unhealthy, one alert is sent per app per run

If `DISCORD_WEBHOOK_URL` is empty, alerts are skipped silently.

## Logging and troubleshooting

The app uses Python `logging` for:

- check start/finish
- unhealthy checks
- Table Storage save success/failure
- Discord alert success/failure

### View logs in Azure

1. Azure Portal -> your Function App
2. **Log stream** for live logs
3. **Monitor -> Logs** for query/history

### Common issues

**Empty `/api/health-report`**

- Check `MONITORED_URLS` is valid JSON with `name` and `url`

**`/api/history` error locally**

- Start **Azurite** before `func start`
- Run `/api/health-report` first to create records

**`/api/dashboard` missing history stats**

- Storage may be unavailable; dashboard still shows latest results with a warning banner

**Discord alerts not sending**

- Confirm `DISCORD_WEBHOOK_URL` is set
- Trigger an unhealthy check or wait for a real failure
- Check Function App logs for Discord errors

**Timer errors locally (`127.0.0.1:10000`)**

- Run Azurite; timer needs storage locally

**Cannot browse tables in Azure Portal**

- Common on student accounts; use `/api/history` or `/api/history-view` instead

## MVP limitations

- In-memory latest cache resets on cold start (history remains in Table Storage)
- History queries load records into memory before sorting (fine for small/medium history)
- Discord alerts are basic webhook messages (no alert dedupe across separate runs)
- No authentication on public HTTP endpoints
- No Application Insights dashboards yet

## Future improvements

- Application Insights alerts and dashboards
- Email/Slack alert channels
- Authenticated admin endpoints
- Per-app SLA reporting
- Bicep or ARM templates for infrastructure (Terraform intentionally not included here)

## Interview talking points

- **Timer + HTTP triggers** share the same `run_health_checks()` helper
- **Dual storage**: memory for fast latest snapshot, Table Storage for durable history
- **Graceful degradation**: dashboard works even if history stats fail
- **Optional integrations**: Discord via env var, no code change needed to disable
- **Observability**: structured logging viewable in Azure Log stream

## License

MIT (or your preferred license)
