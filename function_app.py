"""
Azure Serverless Uptime Monitor

HTTP + timer functions that check monitored URLs, cache latest results,
persist history to Azure Table Storage, and optionally send Discord alerts.

Logging uses Python's logging module. In Azure, view output via:
  Portal -> Function App -> Log stream
  Portal -> Function App -> Monitor -> Logs
"""

import html
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import azure.functions as func
import requests
from azure.data.tables import TableServiceClient

# Create the Function App (Python v2 programming model uses decorators)
app = func.FunctionApp()

# In-memory cache for the most recent health check results.
# Note: this resets when the function app restarts (cold start).
LATEST_RESULTS: list[dict] = []

# How long to wait for each URL before treating it as failed (seconds)
REQUEST_TIMEOUT_SECONDS = 10

# Default Azure Table name for stored uptime check history
DEFAULT_UPTIME_TABLE_NAME = "UptimeChecks"

# Default and max records for /api/history
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 100

# Number of recent checks used for dashboard stats per app
DASHBOARD_STATS_CHECK_COUNT = 10


def _load_monitored_urls() -> list[dict]:
    """
    Read MONITORED_URLS from environment variable.

    Expected JSON format:
      [{"name": "My App", "url": "https://example.com"}, ...]

    Returns an empty list if the variable is missing or invalid.
    """
    raw = os.environ.get("MONITORED_URLS", "[]")
    try:
        urls = json.loads(raw)
        if not isinstance(urls, list):
            logging.warning("MONITORED_URLS must be a JSON array.")
            return []
        return urls
    except json.JSONDecodeError:
        logging.warning("MONITORED_URLS is not valid JSON.")
        return []


def _get_table_name() -> str:
    """Return the configured table name, or the default."""
    return os.environ.get("UPTIME_TABLE_NAME", DEFAULT_UPTIME_TABLE_NAME)


def _get_table_client():
    """
    Connect to Azure Table Storage using AzureWebJobsStorage.
    Creates the table automatically if it does not exist yet.
    """
    connection_string = os.environ.get("AzureWebJobsStorage")
    if not connection_string:
        raise ValueError("AzureWebJobsStorage connection string is not set.")

    table_name = _get_table_name()
    service = TableServiceClient.from_connection_string(connection_string)
    service.create_table_if_not_exists(table_name)
    return service.get_table_client(table_name)


def _save_check_result(result: dict) -> None:
    """Save one health check result to Azure Table Storage."""
    try:
        table_client = _get_table_client()
        app_name = result["app"]
        # RowKey: timestamp prefix (sortable) + short UUID for uniqueness
        row_key = f"{int(time.time() * 1000):013d}_{uuid.uuid4().hex[:8]}"

        entity = {
            "PartitionKey": app_name,
            "RowKey": row_key,
            "app": app_name,
            "url": result["url"],
            "status_code": result["status_code"] if result["status_code"] is not None else -1,
            "response_time_ms": float(result["response_time_ms"]),
            "healthy": bool(result["healthy"]),
            "checked_at": result["checked_at"],
            "error": result["error"] or "",
        }
        table_client.create_entity(entity=entity)
        logging.info("Saved check result for '%s' to table '%s'.", app_name, _get_table_name())
    except Exception as exc:
        # Do not crash the health check if storage fails
        logging.warning("Failed to save check result to Table Storage: %s", exc)


def _compute_app_stats(records: list[dict]) -> dict:
    """
    Compute summary stats from recent check history for one app.
    Expects records sorted newest first (as returned by fetch_history).
    """
    if not records:
        return {
            "check_count": 0,
            "uptime_percent": None,
            "avg_response_time_ms": None,
            "failure_count": 0,
            "latest_error": None,
        }

    healthy_count = sum(1 for record in records if record.get("healthy"))
    total = len(records)
    response_times = [
        record["response_time_ms"]
        for record in records
        if record.get("response_time_ms") is not None
    ]

    latest_error = None
    for record in records:
        if not record.get("healthy") and record.get("error"):
            latest_error = record["error"]
            break

    return {
        "check_count": total,
        "uptime_percent": round((healthy_count / total) * 100, 1),
        "avg_response_time_ms": round(sum(response_times) / len(response_times), 2)
        if response_times
        else None,
        "failure_count": total - healthy_count,
        "latest_error": latest_error,
    }


def _send_discord_alert(result: dict) -> None:
    """
    Send one Discord webhook alert for an unhealthy app.
    Skips silently when DISCORD_WEBHOOK_URL is not configured.
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return

    app_name = result.get("app", "Unknown")
    status_code = result.get("status_code")
    status_display = str(status_code) if status_code is not None else "N/A"
    error_text = result.get("error") or "No error message"

    payload = {
        "content": f"Uptime alert: **{app_name}** is unhealthy",
        "embeds": [
            {
                "title": f"{app_name} health check failed",
                "color": 15158332,
                "fields": [
                    {"name": "URL", "value": result.get("url", "N/A"), "inline": False},
                    {"name": "Status code", "value": status_display, "inline": True},
                    {
                        "name": "Response time (ms)",
                        "value": str(result.get("response_time_ms", "N/A")),
                        "inline": True,
                    },
                    {"name": "Checked at (UTC)", "value": result.get("checked_at", "N/A"), "inline": False},
                    {"name": "Error", "value": error_text, "inline": False},
                ],
            }
        ],
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info("Discord alert sent for unhealthy app '%s'.", app_name)
    except requests.RequestException as exc:
        logging.warning("Failed to send Discord alert for '%s': %s", app_name, exc)


def _send_discord_alerts_for_unhealthy(results: list[dict]) -> None:
    """
    Send Discord alerts for unhealthy apps in this run.
    Sends at most one alert per app to avoid duplicate spam.
    """
    alerted_apps: set[str] = set()

    for result in results:
        if result.get("healthy"):
            continue

        app_name = result.get("app")
        if not app_name or app_name in alerted_apps:
            continue

        alerted_apps.add(app_name)
        logging.warning(
            "Unhealthy app detected: app=%s url=%s status=%s error=%s",
            app_name,
            result.get("url"),
            result.get("status_code"),
            result.get("error"),
        )
        _send_discord_alert(result)


def _entity_to_record(entity: dict) -> dict:
    """Convert a Table Storage entity back into a history record dict."""
    status_code = entity.get("status_code")
    if status_code == -1:
        status_code = None

    error = entity.get("error") or None

    return {
        "app": entity.get("app") or entity.get("PartitionKey"),
        "url": entity.get("url"),
        "status_code": status_code,
        "response_time_ms": entity.get("response_time_ms"),
        "healthy": entity.get("healthy"),
        "checked_at": entity.get("checked_at"),
        "error": error,
    }


def _escape_odata_value(value: str) -> str:
    """Escape single quotes for OData filter strings."""
    return value.replace("'", "''")


def fetch_history(app_filter: str | None = None, limit: int = DEFAULT_HISTORY_LIMIT) -> list[dict]:
    """
    Read recent check history from Azure Table Storage.
    Results are sorted newest first.
    """
    table_client = _get_table_client()

    if app_filter:
        safe_app = _escape_odata_value(app_filter)
        query_filter = f"PartitionKey eq '{safe_app}'"
        entities = table_client.query_entities(query_filter=query_filter)
    else:
        entities = table_client.list_entities()

    records = [_entity_to_record(dict(entity)) for entity in entities]
    records.sort(key=lambda item: item.get("checked_at") or "", reverse=True)
    return records[:limit]


def _json_response(payload: dict, status_code: int = 200) -> func.HttpResponse:
    """Return a JSON HttpResponse."""
    return func.HttpResponse(
        body=json.dumps(payload, indent=2),
        status_code=status_code,
        mimetype="application/json",
    )


def _check_single_url(name: str, url: str) -> dict:
    """
    Perform one HTTP GET against a URL and build a result dictionary.

    A site is considered healthy when the status code is 2xx or 3xx.
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        status_code = response.status_code
        healthy = 200 <= status_code < 400

        if not healthy:
            logging.warning(
                "Unhealthy response: app=%s url=%s status_code=%s",
                name,
                url,
                status_code,
            )

        return {
            "app": name,
            "url": url,
            "status_code": status_code,
            "response_time_ms": elapsed_ms,
            "healthy": healthy,
            "checked_at": checked_at,
            "error": None,
        }
    except requests.RequestException as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        logging.warning(
            "Health check request failed: app=%s url=%s error=%s",
            name,
            url,
            exc,
        )
        return {
            "app": name,
            "url": url,
            "status_code": None,
            "response_time_ms": elapsed_ms,
            "healthy": False,
            "checked_at": checked_at,
            "error": str(exc),
        }


def run_health_checks() -> list[dict]:
    """
    Check every URL listed in MONITORED_URLS and return the results.

    Each item in MONITORED_URLS should have "name" and "url" keys.
    Entries missing either key are skipped with a warning.
    """
    monitored = _load_monitored_urls()
    results: list[dict] = []

    logging.info("Starting health checks for %d monitored URL(s).", len(monitored))

    for entry in monitored:
        name = entry.get("name")
        url = entry.get("url")
        if not name or not url:
            logging.warning("Skipping entry missing 'name' or 'url': %s", entry)
            continue
        result = _check_single_url(name, url)
        results.append(result)
        _save_check_result(result)

    _send_discord_alerts_for_unhealthy(results)
    logging.info("Health checks completed for %d URL(s).", len(results))
    return results


def _build_dashboard_html(
    results: list[dict],
    stats_by_app: dict[str, dict] | None = None,
    storage_warning: str | None = None,
) -> str:
    """Build a simple HTML page with one card per monitored app and optional history stats."""
    stats_by_app = stats_by_app or {}
    cards_html = []
    warning_html = ""
    if storage_warning:
        warning_html = f'<p class="warning">{html.escape(storage_warning)}</p>'

    for item in results:
        healthy = item.get("healthy", False)
        badge_class = "badge healthy" if healthy else "badge unhealthy"
        badge_text = "Healthy" if healthy else "Unhealthy"
        status_code = item.get("status_code")
        status_display = html.escape(str(status_code)) if status_code is not None else "N/A"
        error = item.get("error")
        error_row = ""
        if error:
            error_row = (
                f'<p class="error"><strong>Latest error:</strong> '
                f"{html.escape(str(error))}</p>"
            )

        app_name = str(item.get("app", "Unknown"))
        stats = stats_by_app.get(app_name, {})
        stats_block = ""

        if stats.get("check_count", 0) > 0:
            uptime_display = (
                f"{stats['uptime_percent']}%"
                if stats.get("uptime_percent") is not None
                else "N/A"
            )
            avg_display = (
                f"{stats['avg_response_time_ms']} ms"
                if stats.get("avg_response_time_ms") is not None
                else "N/A"
            )
            history_error = stats.get("latest_error")
            history_error_row = ""
            if history_error:
                history_error_row = (
                    f'<p class="stats-error"><strong>Recent error:</strong> '
                    f"{html.escape(str(history_error))}</p>"
                )

            stats_block = f"""
                <div class="stats">
                    <p><strong>History stats (last {DASHBOARD_STATS_CHECK_COUNT} checks):</strong></p>
                    <p>Uptime: {html.escape(uptime_display)}</p>
                    <p>Average response time: {html.escape(avg_display)}</p>
                    <p>Failure count: {html.escape(str(stats.get('failure_count', 0)))}</p>
                    {history_error_row}
                </div>
            """
        elif storage_warning is None:
            stats_block = (
                '<div class="stats"><p class="stats-empty">No stored history yet for this app.</p></div>'
            )

        cards_html.append(
            f"""
            <div class="card">
                <div class="card-header">
                    <h2>{html.escape(app_name)}</h2>
                    <span class="{badge_class}">{badge_text}</span>
                </div>
                <p><strong>URL:</strong> <a href="{html.escape(str(item.get("url", "")))}" target="_blank" rel="noopener">{html.escape(str(item.get("url", "")))}</a></p>
                <p><strong>Status code:</strong> {status_display}</p>
                <p><strong>Response time:</strong> {html.escape(str(item.get("response_time_ms", "N/A")))} ms</p>
                <p><strong>Checked at:</strong> {html.escape(str(item.get("checked_at", "N/A")))}</p>
                {error_row}
                {stats_block}
            </div>
            """
        )

    if not cards_html:
        cards_html.append(
            '<p class="empty">No monitored URLs configured. Set MONITORED_URLS in app settings.</p>'
        )

    cards_block = "\n".join(cards_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Uptime Monitor Dashboard</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            background: #f4f6f8;
            color: #1a1a1a;
            margin: 0;
            padding: 24px;
        }}
        h1 {{
            margin-top: 0;
        }}
        .subtitle {{
            color: #555;
            margin-bottom: 24px;
        }}
        .cards {{
            display: grid;
            gap: 16px;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        }}
        .card {{
            background: #fff;
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 16px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
        }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 12px;
        }}
        .card h2 {{
            margin: 0;
            font-size: 1.1rem;
        }}
        .badge {{
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 0.85rem;
            font-weight: bold;
            white-space: nowrap;
        }}
        .badge.healthy {{
            background: #d4edda;
            color: #155724;
        }}
        .badge.unhealthy {{
            background: #f8d7da;
            color: #721c24;
        }}
        .error {{
            color: #721c24;
            background: #f8d7da;
            padding: 8px;
            border-radius: 4px;
        }}
        .warning {{
            color: #856404;
            background: #fff3cd;
            border: 1px solid #ffeeba;
            padding: 12px;
            border-radius: 4px;
            margin-bottom: 16px;
        }}
        .stats {{
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid #eee;
            color: #444;
            font-size: 0.95rem;
        }}
        .stats-error {{
            color: #721c24;
            background: #f8d7da;
            padding: 8px;
            border-radius: 4px;
        }}
        .stats-empty {{
            color: #666;
            margin: 0;
        }}
        .empty {{
            color: #555;
        }}
        .footer {{
            margin-top: 24px;
        }}
        a {{
            color: #0066cc;
        }}
    </style>
</head>
<body>
    <h1>Uptime Monitor Dashboard</h1>
    <p class="subtitle">Latest health check results and recent history stats for your monitored apps.</p>
    {warning_html}
    <div class="cards">
        {cards_block}
    </div>
    <p class="footer"><a href="/api/health-report">View JSON health report</a> | <a href="/api/history">View JSON history</a> | <a href="/api/history-view">View history table</a></p>
</body>
</html>"""


def _parse_history_params(req: func.HttpRequest) -> tuple[str | None, int, func.HttpResponse | None]:
    """
    Read and validate ?app= and ?limit= query params for history endpoints.
    Returns (app_filter, limit, error_response). error_response is None when valid.
    """
    app_filter = req.params.get("app")
    limit_param = req.params.get("limit", str(DEFAULT_HISTORY_LIMIT))

    try:
        limit = int(limit_param)
    except ValueError:
        error = _json_response(
            {
                "error": "Invalid limit parameter. limit must be a whole number.",
                "example": "/api/history-view?limit=10&app=Kairos",
            },
            status_code=400,
        )
        return app_filter, DEFAULT_HISTORY_LIMIT, error

    if limit < 1:
        error = _json_response(
            {"error": "Invalid limit parameter. limit must be at least 1."},
            status_code=400,
        )
        return app_filter, DEFAULT_HISTORY_LIMIT, error

    return app_filter, min(limit, MAX_HISTORY_LIMIT), None


def _build_history_html(records: list[dict], app_filter: str | None, limit: int) -> str:
    """Build an HTML page with a table of stored uptime check history."""
    rows_html = []

    for item in records:
        healthy = item.get("healthy", False)
        badge_class = "badge healthy" if healthy else "badge unhealthy"
        badge_text = "Healthy" if healthy else "Unhealthy"
        status_code = item.get("status_code")
        status_display = html.escape(str(status_code)) if status_code is not None else "N/A"
        error = item.get("error")
        error_display = html.escape(str(error)) if error else "—"

        rows_html.append(
            f"""
            <tr>
                <td>{html.escape(str(item.get("app", "Unknown")))}</td>
                <td><a href="{html.escape(str(item.get("url", "")))}" target="_blank" rel="noopener">{html.escape(str(item.get("url", "")))}</a></td>
                <td><span class="{badge_class}">{badge_text}</span></td>
                <td>{status_display}</td>
                <td>{html.escape(str(item.get("response_time_ms", "N/A")))} ms</td>
                <td>{html.escape(str(item.get("checked_at", "N/A")))}</td>
                <td>{error_display}</td>
            </tr>
            """
        )

    if not rows_html:
        rows_html.append(
            '<tr><td colspan="7" class="empty">No history found yet. Visit <a href="/api/health-report">/api/health-report</a> to run checks first.</td></tr>'
        )

    table_body = "\n".join(rows_html)
    filter_note = f"Showing app: <strong>{html.escape(app_filter)}</strong>" if app_filter else "Showing all apps"
    table_name = html.escape(_get_table_name())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Uptime Monitor History</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            background: #f4f6f8;
            color: #1a1a1a;
            margin: 0;
            padding: 24px;
        }}
        h1 {{
            margin-top: 0;
        }}
        .subtitle {{
            color: #555;
            margin-bottom: 16px;
        }}
        .meta {{
            color: #555;
            margin-bottom: 16px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: #fff;
            border: 1px solid #ddd;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
        }}
        th, td {{
            padding: 12px;
            border-bottom: 1px solid #eee;
            text-align: left;
            vertical-align: top;
        }}
        th {{
            background: #fafafa;
            font-size: 0.9rem;
        }}
        tr:last-child td {{
            border-bottom: none;
        }}
        .badge {{
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 0.85rem;
            font-weight: bold;
            white-space: nowrap;
        }}
        .badge.healthy {{
            background: #d4edda;
            color: #155724;
        }}
        .badge.unhealthy {{
            background: #f8d7da;
            color: #721c24;
        }}
        .empty {{
            text-align: center;
            color: #555;
        }}
        .footer {{
            margin-top: 24px;
        }}
        a {{
            color: #0066cc;
        }}
    </style>
</head>
<body>
    <h1>Uptime Monitor History</h1>
    <p class="subtitle">Stored check history from Azure Table Storage.</p>
    <p class="meta">{filter_note} · Limit: {limit} · Table: {table_name}</p>
    <table>
        <thead>
            <tr>
                <th>App</th>
                <th>URL</th>
                <th>Status</th>
                <th>Code</th>
                <th>Response Time</th>
                <th>Checked At (UTC)</th>
                <th>Error</th>
            </tr>
        </thead>
        <tbody>
            {table_body}
        </tbody>
    </table>
    <p class="footer">
        <a href="/api/dashboard">Back to dashboard</a> |
        <a href="/api/history?limit={limit}">View JSON history</a>
    </p>
</body>
</html>"""


@app.timer_trigger(
    schedule="0 0 * * * *",  # At minute 0 of every hour (NCRONTAB format)
    arg_name="timer",
    run_on_startup=False,
)
def scheduled_health_check(timer: func.TimerRequest) -> None:
    """
    Runs automatically every hour.

    Fetches each monitored URL and stores results in LATEST_RESULTS and Table Storage.
    Sends Discord alerts for unhealthy apps when configured.

    View logs in Azure Portal: Function App -> Log stream / Monitor -> Logs.
    """
    global LATEST_RESULTS
    logging.info("Timer fired. past_due=%s", timer.past_due)
    LATEST_RESULTS = run_health_checks()


@app.route(route="health-report", auth_level=func.AuthLevel.ANONYMOUS)
def health_report(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP endpoint: GET /api/health-report

    Returns the latest health check results as JSON.
    If no timer run has happened yet (cold start), runs checks on demand.
    """
    global LATEST_RESULTS

    if not LATEST_RESULTS:
        logging.info("Cache empty; running health checks on demand.")
        LATEST_RESULTS = run_health_checks()

    body = json.dumps(
        {
            "results": LATEST_RESULTS,
            "count": len(LATEST_RESULTS),
        },
        indent=2,
    )

    return func.HttpResponse(
        body=body,
        status_code=200,
        mimetype="application/json",
    )


@app.route(route="dashboard", auth_level=func.AuthLevel.ANONYMOUS)
def dashboard(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP endpoint: GET /api/dashboard

    Returns a simple HTML page showing the latest health check results plus
    recent history stats from Azure Table Storage when available.
    """
    global LATEST_RESULTS

    if not LATEST_RESULTS:
        logging.info("Cache empty; running health checks for dashboard.")
        LATEST_RESULTS = run_health_checks()

    stats_by_app: dict[str, dict] = {}
    storage_warning = None

    try:
        for item in LATEST_RESULTS:
            app_name = item.get("app")
            if not app_name:
                continue
            app_history = fetch_history(
                app_filter=app_name,
                limit=DASHBOARD_STATS_CHECK_COUNT,
            )
            stats_by_app[app_name] = _compute_app_stats(app_history)
    except Exception as exc:
        logging.warning("Dashboard could not load history stats: %s", exc)
        storage_warning = (
            "History stats are unavailable right now. "
            "Latest check results are still shown below."
        )

    html_body = _build_dashboard_html(
        LATEST_RESULTS,
        stats_by_app=stats_by_app,
        storage_warning=storage_warning,
    )

    return func.HttpResponse(
        body=html_body,
        status_code=200,
        mimetype="text/html",
    )


@app.route(route="history", auth_level=func.AuthLevel.ANONYMOUS)
def history(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP endpoint: GET /api/history

    Returns recent stored uptime checks from Azure Table Storage.
    Query params:
      ?app=Kairos  - filter by app name (PartitionKey)
      ?limit=10    - max records to return (default 50)
    """
    app_filter, limit, error_response = _parse_history_params(req)
    if error_response:
        return error_response

    try:
        records = fetch_history(app_filter=app_filter, limit=limit)
    except ValueError as exc:
        return _json_response(
            {"error": str(exc), "hint": "Set AzureWebJobsStorage in app settings."},
            status_code=500,
        )
    except Exception as exc:
        logging.exception("Failed to read history from Table Storage.")
        return _json_response(
            {
                "error": "Failed to read history from Azure Table Storage.",
                "details": str(exc),
            },
            status_code=500,
        )

    return _json_response(
        {
            "results": records,
            "count": len(records),
            "app_filter": app_filter,
            "limit": limit,
            "table_name": _get_table_name(),
        }
    )


@app.route(route="history-view", auth_level=func.AuthLevel.ANONYMOUS)
def history_view(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP endpoint: GET /api/history-view

    Returns stored uptime check history as an HTML table.
    Supports the same query params as /api/history:
      ?app=Kairos  - filter by app name
      ?limit=10    - max records (default 50)
    """
    app_filter, limit, error_response = _parse_history_params(req)
    if error_response:
        # Return a simple HTML error page for browser users
        return func.HttpResponse(
            body=f"<html><body><h1>Invalid request</h1><p>Check your limit parameter.</p><p><a href='/api/history-view'>Try again</a></p></body></html>",
            status_code=400,
            mimetype="text/html",
        )

    try:
        records = fetch_history(app_filter=app_filter, limit=limit)
    except ValueError as exc:
        return func.HttpResponse(
            body=(
                f"<html><body><h1>Storage error</h1>"
                f"<p>{html.escape(str(exc))}</p>"
                f"<p><a href='/api/dashboard'>Back to dashboard</a></p></body></html>"
            ),
            status_code=500,
            mimetype="text/html",
        )
    except Exception as exc:
        logging.exception("Failed to read history from Table Storage.")
        return func.HttpResponse(
            body=(
                f"<html><body><h1>Storage error</h1>"
                f"<p>{html.escape(str(exc))}</p>"
                f"<p><a href='/api/dashboard'>Back to dashboard</a></p></body></html>"
            ),
            status_code=500,
            mimetype="text/html",
        )

    html_body = _build_history_html(records, app_filter, limit)

    return func.HttpResponse(
        body=html_body,
        status_code=200,
        mimetype="text/html",
    )
