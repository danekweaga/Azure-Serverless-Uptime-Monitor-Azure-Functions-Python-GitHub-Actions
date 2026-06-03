"""
Azure Serverless Uptime Monitor (MVP)

Two functions:
  1. scheduled_health_check  - timer trigger, runs every hour
  2. health_report           - HTTP trigger, returns latest results as JSON

Results are stored in memory (LATEST_RESULTS). On a cold start before the
first timer run, health-report will run checks on demand.
"""

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
