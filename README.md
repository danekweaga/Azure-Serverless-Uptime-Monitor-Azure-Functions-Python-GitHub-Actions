# Azure Serverless Uptime Monitor (MVP)

A beginner-friendly **serverless uptime monitor** built with **Python Azure Functions**. It checks a list of URLs every hour and exposes a simple HTTP API with the latest results.

Perfect for explaining in an interview: timer trigger, HTTP API, environment-based config, and CI/CD with GitHub Actions.

## How it works

```text
MONITORED_URLS (env var, JSON)
        |
        v
+---------------------------+
|  scheduled_health_check   |  Timer: every hour
|  (updates in-memory cache)|
+---------------------------+
        |
        v
   LATEST_RESULTS (in memory)
        |
        v
+---------------------------+
|  GET /api/health-report   |  HTTP: returns JSON
+---------------------------+
```

- **Timer function** (`scheduled_health_check`): runs every hour, checks each URL, stores results in memory.
- **HTTP function** (`health-report`): returns cached results. If the cache is empty (e.g. right after a cold start), it runs checks on demand.

> **MVP limitation:** Results live in memory and are lost when the app restarts. Later you can add Azure Table Storage for persistence.

## Project structure

```text
.
├── function_app.py              # Both functions + health check logic
├── host.json                    # Azure Functions host configuration
├── requirements.txt             # Python dependencies
├── local.settings.json.example  # Copy to local.settings.json for local dev
├── .github/workflows/deploy.yml # GitHub Actions deployment
└── README.md
```

## Prerequisites

- [Python 3.11](https://www.python.org/downloads/)
- [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (for creating Azure resources)
- An Azure subscription (free tier is fine for learning)

## Local setup

### 1. Clone and create a virtual environment

```bash
git clone <your-repo-url>
cd Azure-Serverless-Uptime-Monitor-Azure-Functions-Python-GitHub-Actions

python -m venv .venv

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure local settings

Copy the example file (this file is **not** committed to git):

```bash
# Windows
copy local.settings.json.example local.settings.json

# macOS / Linux
cp local.settings.json.example local.settings.json
```

Edit `local.settings.json` and set `MONITORED_URLS` to the sites you want to monitor.

### 4. Run locally

```bash
func start
```

You should see output like:

```text
Functions:
  health_report: [GET] http://localhost:7071/api/health-report
  scheduled_health_check: timerTrigger
```

### 5. Test the health report

Open in a browser or use curl:

```bash
curl http://localhost:7071/api/health-report
```

Example response:

```json
{
  "results": [
    {
      "app": "Example",
      "url": "https://example.com",
      "status_code": 200,
      "response_time_ms": 245.12,
      "healthy": true,
      "checked_at": "2026-06-03T17:00:00.123456+00:00",
      "error": null
    }
  ],
  "count": 1
}
```

The timer runs every hour locally as well. For faster testing during development, you can temporarily change the schedule in `function_app.py` (see comments in that file).

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FUNCTIONS_WORKER_RUNTIME` | Yes | Must be `python` |
| `AzureWebJobsStorage` | Yes | Storage connection for the Functions runtime |
| `MONITORED_URLS` | Yes | JSON array of sites to monitor |

### `MONITORED_URLS` format

```json
[
  {"name": "My API", "url": "https://api.example.com/health"},
  {"name": "Marketing Site", "url": "https://www.example.com"}
]
```

- **name**: Friendly app name shown in the report (`app` field in results).
- **url**: Full URL to request (GET).

## Deploy to Azure (manual)

### 1. Create resources

```bash
# Login
az login

# Variables (change these)
RESOURCE_GROUP="rg-uptime-monitor"
LOCATION="eastus"
STORAGE_ACCOUNT="stuptimemonitor001"   # must be globally unique, lowercase
FUNCTION_APP="func-uptime-monitor-001"  # must be globally unique

az group create --name $RESOURCE_GROUP --location $LOCATION

az storage account create \
  --name $STORAGE_ACCOUNT \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION \
  --sku Standard_LRS

az functionapp create \
  --resource-group $RESOURCE_GROUP \
  --consumption-plan-location $LOCATION \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --name $FUNCTION_APP \
  --storage-account $STORAGE_ACCOUNT \
  --os-type Linux
```

### 2. Set application settings

```bash
az functionapp config appsettings set \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  --settings \
    FUNCTIONS_WORKER_RUNTIME=python \
    MONITORED_URLS='[{"name":"Example","url":"https://example.com"}]'
```

### 3. Deploy from your machine

```bash
func azure functionapp publish $FUNCTION_APP
```

### 4. Call the live endpoint

```bash
curl https://$FUNCTION_APP.azurewebsites.net/api/health-report
```

## Deploy with GitHub Actions

The workflow in [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) deploys on push to `main` or when run manually.

### Setup

1. In Azure Portal, open your Function App → **Get publish profile** → download the file.
2. In GitHub: **Settings** → **Secrets and variables** → **Actions**:
   - **Secret:** `AZURE_FUNCTIONAPP_PUBLISH_PROFILE` — paste the entire contents of the publish profile file.
   - **Variable:** `AZURE_FUNCTIONAPP_NAME` — your Function App name (e.g. `func-uptime-monitor-001`).
3. Push to `main` or run the workflow from the **Actions** tab.

### Set `MONITORED_URLS` in Azure

GitHub Actions deploys code only; configure URLs in Azure:

```bash
az functionapp config appsettings set \
  --name $FUNCTION_APP \
  --resource-group $RESOURCE_GROUP \
  --settings MONITORED_URLS='[{"name":"Example","url":"https://example.com"}]'
```

Or use **Azure Portal** → Function App → **Environment variables**.

## Troubleshooting

### `health-report` returns empty results

- Check that `MONITORED_URLS` is set and valid JSON in `local.settings.json` (local) or Azure App Settings (cloud).
- Each entry needs both `"name"` and `"url"`.
- Open **Logs** in the portal or run `func start` and watch the console for warnings.

### `ModuleNotFoundError: No module named 'requests'`

```bash
pip install -r requirements.txt
```

For Azure, ensure `requirements.txt` is at the project root (it is in this repo).

### Timer does not seem to run locally

- Timer triggers need `AzureWebJobsStorage` configured (use `UseDevelopmentStorage=true` with [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) or a real storage connection string).
- Schedules use NCRONTAB: `0 0 * * * *` = top of every hour. Use `health-report` to trigger an on-demand check anytime.

### Cold start / stale data

- In-memory cache clears when Azure restarts your function instance.
- First call to `health-report` after restart runs checks immediately if the cache is empty.

### Deployment fails in GitHub Actions

- Verify `AZURE_FUNCTIONAPP_PUBLISH_PROFILE` secret is the full XML from the publish profile.
- Verify `AZURE_FUNCTIONAPP_NAME` variable matches the app name exactly.
- Confirm the Function App uses **Python 3.11** and **Functions v4**.

### URL marked unhealthy but works in browser

- Some sites block non-browser user agents or require HTTPS redirects.
- Check the `error` field in the JSON response for timeout or connection errors.
- Default timeout is 10 seconds (see `REQUEST_TIMEOUT_SECONDS` in `function_app.py`).

## Interview talking points

- **Serverless**: No VM to manage; Azure runs your code on a schedule and on HTTP requests.
- **Separation of concerns**: `run_health_checks()` is shared by timer and HTTP triggers.
- **Config via environment**: `MONITORED_URLS` keeps code generic; change sites without redeploying (in Azure App Settings).
- **Trade-offs**: In-memory storage is simple but not durable—good MVP, then add Table Storage.
- **CI/CD**: GitHub Actions + publish profile is a common, interview-friendly deployment story.

## Next steps (not in MVP)

- Persist results in **Azure Table Storage**
- Add **Application Insights** for alerts and dashboards
- Infrastructure as Code with **Terraform** or **Bicep**
- Email/Slack notifications when `healthy` is `false`

## License

MIT (or your preferred license)
