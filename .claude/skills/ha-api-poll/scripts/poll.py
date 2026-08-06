#!/usr/bin/env python3
"""Poll Home Assistant REST API endpoints and print the JSON response.

Credentials come from the environment (HA_URL, HA_TOKEN). If they aren't
already set, this script looks for a .env file in the project root (two
levels up from this script) and loads HA_URL / HA_TOKEN from it.

Usage examples:
    poll.py ping
    poll.py config
    poll.py states
    poll.py states light.office
    poll.py history sensor.temperature --start 2026-07-31T00:00:00+00:00
    poll.py logbook --entity light.office
    poll.py calendars
    poll.py calendar calendar.home --start 2026-08-01T00:00:00 --end 2026-08-08T00:00:00
    poll.py get /api/services      # generic escape hatch for any GET endpoint
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def load_env_file():
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_session():
    load_env_file()
    base_url = os.environ.get("HA_URL")
    token = os.environ.get("HA_TOKEN")
    if not base_url or not token:
        sys.exit(
            "Missing HA_URL and/or HA_TOKEN. Set them as environment variables "
            f"or in {PROJECT_ROOT / '.env'}."
        )
    session = requests.Session()
    session.headers.update(
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    )
    return session, base_url.rstrip("/")


def request(session, base_url, path, params=None, as_json=True):
    url = f"{base_url}{path}"
    resp = session.get(url, params=params, timeout=15)
    if resp.status_code == 404:
        sys.exit(f"404 Not Found: {url}")
    if resp.status_code == 401:
        sys.exit("401 Unauthorized — check HA_TOKEN is valid and not expired.")
    resp.raise_for_status()
    return resp.json() if as_json else resp.text


def output(data):
    if isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Poll Home Assistant REST API endpoints.")
    sub = parser.add_subparsers(dest="resource", required=True)

    sub.add_parser("ping", help="Check the API is up (/api/)")
    sub.add_parser("config", help="System configuration (/api/config)")
    sub.add_parser("components", help="Loaded components (/api/components)")
    sub.add_parser("events", help="Event types and listener counts (/api/events)")
    sub.add_parser("services", help="Available services by domain (/api/services)")
    sub.add_parser("errors", help="Plaintext error log (/api/error_log)")
    sub.add_parser("calendars", help="List calendar entities (/api/calendars)")

    p_states = sub.add_parser("states", help="All entity states, or one entity's state")
    p_states.add_argument("entity_id", nargs="?", help="e.g. light.office")

    p_history = sub.add_parser("history", help="History for an entity (/api/history/period)")
    p_history.add_argument("entity_id", help="e.g. sensor.temperature")
    p_history.add_argument("--start", help="ISO timestamp; defaults to 1 day ago server-side")
    p_history.add_argument("--end", help="ISO end_time")
    p_history.add_argument("--minimal-response", action="store_true")
    p_history.add_argument("--no-attributes", action="store_true")
    p_history.add_argument("--significant-changes-only", action="store_true")

    p_logbook = sub.add_parser("logbook", help="Logbook entries (/api/logbook)")
    p_logbook.add_argument("--start", help="ISO timestamp; defaults to 1 day ago server-side")
    p_logbook.add_argument("--entity", help="Filter to one entity_id")
    p_logbook.add_argument("--end", help="ISO end_time")

    p_calendar = sub.add_parser("calendar", help="Events for one calendar entity")
    p_calendar.add_argument("entity_id", help="e.g. calendar.home")
    p_calendar.add_argument("--start", required=True, help="ISO start timestamp")
    p_calendar.add_argument("--end", required=True, help="ISO end timestamp")

    p_get = sub.add_parser("get", help="Generic GET against any /api/... path")
    p_get.add_argument("path", help="e.g. /api/states/light.office")
    p_get.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE",
        help="Query param, repeatable",
    )

    args = parser.parse_args()
    session, base_url = get_session()

    if args.resource == "ping":
        output(request(session, base_url, "/api/"))
    elif args.resource == "config":
        output(request(session, base_url, "/api/config"))
    elif args.resource == "components":
        output(request(session, base_url, "/api/components"))
    elif args.resource == "events":
        output(request(session, base_url, "/api/events"))
    elif args.resource == "services":
        output(request(session, base_url, "/api/services"))
    elif args.resource == "errors":
        output(request(session, base_url, "/api/error_log", as_json=False))
    elif args.resource == "calendars":
        output(request(session, base_url, "/api/calendars"))
    elif args.resource == "states":
        if args.entity_id:
            output(request(session, base_url, f"/api/states/{quote(args.entity_id)}"))
        else:
            output(request(session, base_url, "/api/states"))
    elif args.resource == "history":
        timestamp = f"/{args.start}" if args.start else ""
        params = {"filter_entity_id": args.entity_id}
        if args.end:
            params["end_time"] = args.end
        if args.minimal_response:
            params["minimal_response"] = ""
        if args.no_attributes:
            params["no_attributes"] = ""
        if args.significant_changes_only:
            params["significant_changes_only"] = ""
        output(request(session, base_url, f"/api/history/period{timestamp}", params=params))
    elif args.resource == "logbook":
        timestamp = f"/{args.start}" if args.start else ""
        params = {}
        if args.entity:
            params["entity"] = args.entity
        if args.end:
            params["end_time"] = args.end
        output(request(session, base_url, f"/api/logbook{timestamp}", params=params))
    elif args.resource == "calendar":
        params = {"start": args.start, "end": args.end}
        output(request(session, base_url, f"/api/calendars/{quote(args.entity_id)}", params=params))
    elif args.resource == "get":
        params = {}
        for kv in args.param:
            key, _, value = kv.partition("=")
            params[key] = value
        path = args.path if args.path.startswith("/") else f"/{args.path}"
        output(request(session, base_url, path, params=params))


if __name__ == "__main__":
    main()
