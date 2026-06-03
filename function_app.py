"""
Azure Serverless Uptime Monitor (MVP)

Three functions:
  1. scheduled_health_check  - timer trigger, runs every hour
  2. health_report           - HTTP trigger, returns latest results as JSON
  3. dashboard               - HTTP trigger, returns a simple HTML status page

Results are stored in memory (LATEST_RESULTS). On a cold start before the
first timer run, health-report will run checks on demand.
"""

import html
import json
import logging
import os
import time
from datetime import datetime, timezone

import azure.functions as func
import requests

# Create the Function App (Python v2 programming model uses decorators)
app = func.FunctionApp()

# In-memory cache for the most recent health check results.
# Note: this resets when the function app restarts (cold start).
LATEST_RESULTS: list[dict] = []

# How long to wait for each URL before treating it as failed (seconds)
REQUEST_TIMEOUT_SECONDS = 10


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

    for entry in monitored:
        name = entry.get("name")
        url = entry.get("url")
        if not name or not url:
            logging.warning("Skipping entry missing 'name' or 'url': %s", entry)
            continue
        results.append(_check_single_url(name, url))

    logging.info("Health checks completed for %d URL(s).", len(results))
    return results


def _build_dashboard_html(results: list[dict]) -> str:
    """Build a simple HTML page with one card per monitored app."""
    cards_html = []

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
                f'<p class="error"><strong>Error:</strong> '
                f"{html.escape(str(error))}</p>"
            )

        cards_html.append(
            f"""
            <div class="card">
                <div class="card-header">
                    <h2>{html.escape(str(item.get("app", "Unknown")))}</h2>
                    <span class="{badge_class}">{badge_text}</span>
                </div>
                <p><strong>URL:</strong> <a href="{html.escape(str(item.get("url", "")))}" target="_blank" rel="noopener">{html.escape(str(item.get("url", "")))}</a></p>
                <p><strong>Status code:</strong> {status_display}</p>
                <p><strong>Response time:</strong> {html.escape(str(item.get("response_time_ms", "N/A")))} ms</p>
                <p><strong>Checked at:</strong> {html.escape(str(item.get("checked_at", "N/A")))}</p>
                {error_row}
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
    <p class="subtitle">Latest health check results for your monitored apps.</p>
    <div class="cards">
        {cards_block}
    </div>
    <p class="footer"><a href="/api/health-report">View JSON health report</a></p>
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

    Fetches each monitored URL and stores results in LATEST_RESULTS.
    """
    global LATEST_RESULTS
    logging.info("Timer fired at %s", timer.past_due)
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

    Returns a simple HTML page showing the latest health check results.
    Reuses cached results; runs checks on demand if the cache is empty.
    """
    global LATEST_RESULTS

    if not LATEST_RESULTS:
        logging.info("Cache empty; running health checks for dashboard.")
        LATEST_RESULTS = run_health_checks()

    html_body = _build_dashboard_html(LATEST_RESULTS)

    return func.HttpResponse(
        body=html_body,
        status_code=200,
        mimetype="text/html",
    )
