# Home Assistant REST API Reference

Full docs: https://developers.home-assistant.io/docs/api/rest/

Home Assistant exposes a REST API on the same port as the frontend (default
`8123`), base URL `http://IP_ADDRESS:8123/api/`. All payloads are JSON.

**Auth**: every request needs header `Authorization: Bearer <LONG_LIVED_ACCESS_TOKEN>`
(generate the token from the HA profile page in the web UI).

## Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/` | Health check → `{"message": "API running."}` |
| GET | `/api/config` | System config (components, coords, timezone, version) |
| GET | `/api/components` | List loaded components |
| GET | `/api/events` | Event types + listener counts |
| GET | `/api/services` | Available services by domain |
| GET | `/api/error_log` | Plaintext error log |
| GET | `/api/states` | All entity states |
| GET | `/api/states/<entity_id>` | Single entity state (404 if missing) |
| GET | `/api/history/period/<timestamp>` | History; needs `filter_entity_id`, optional `end_time`, `minimal_response`, `no_attributes`, `significant_changes_only` |
| GET | `/api/logbook/<timestamp>` | Logbook entries; optional `entity`, `end_time` |
| GET | `/api/camera_proxy/<camera_entity_id>` | Camera snapshot |
| GET | `/api/calendars` | List calendars |
| GET | `/api/calendars/<entity_id>` | Calendar events; needs `start`/`end` (ISO) |
| POST | `/api/states/<entity_id>` | Create/update state (`state`, optional `attributes`) → 200 update / 201 new |
| POST | `/api/events/<event_type>` | Fire custom event |
| POST | `/api/services/<domain>/<service>` | Call a service (optional `?return_response`) |
| POST | `/api/template` | Render a Jinja2 template |
| POST | `/api/config/core/check_config` | Validate `configuration.yaml` |
| POST | `/api/intent/handle` | Handle an intent |
| DELETE | `/api/states/<entity_id>` | Delete entity state |

**Status codes**: 200/201 success (201 = new entity created), 400 bad
request, 401 unauthorized, 404 not found, 405 method not allowed.

**Example**:
```bash
curl -X GET -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" http://localhost:8123/api/states
```

## What `scripts/poll.py` covers vs. doesn't

`poll.py` only implements the GET (read) endpoints above, since this skill
is for polling/inspecting state rather than mutating it — see the table in
`SKILL.md` for the exact command-to-endpoint mapping.

The POST/DELETE endpoints (`/api/states/<id>` write, `/api/events/<type>`,
`/api/services/<domain>/<service>`, `/api/template`,
`/api/config/core/check_config`, `/api/intent/handle`) are **not** wrapped by
this skill. If a task needs one of those, either use `poll.py get <path>`
(GET only) as a reference for auth/URL handling, or write the POST call
directly with `requests`/`curl` using the same `Authorization: Bearer
$HA_TOKEN` header and `HA_URL` base from `.env`.
