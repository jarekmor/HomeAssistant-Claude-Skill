---
name: ha-api-poll
description: Poll the Home Assistant REST API (states, config, history, logbook, events, services, calendars, or any raw endpoint) for this project's HA instance and print the JSON response. Use this whenever the user wants to check an entity's current state, look up recent history or logbook activity, inspect HA config/components/services, or otherwise query the Home Assistant REST API directly — even if they just say "check the light state" or "what's HA's config" rather than naming the API explicitly.
---

# Home Assistant API Poller

Query the Home Assistant REST API for this project (base URL and token read
from `HA_URL` / `HA_TOKEN` in the project's `.env`) and print the response as
JSON.

Run the bundled script rather than writing ad-hoc `curl`/`requests` calls —
it already handles auth headers, error codes (401/404), and loading `.env`.

```bash
python3 .claude/skills/ha-api-poll/scripts/poll.py <resource> [args...]
```

## Resources

| Command | Endpoint | Notes |
|---|---|---|
| `ping` | `GET /api/` | Health check |
| `config` | `GET /api/config` | Components, coords, timezone, version |
| `components` | `GET /api/components` | Loaded components |
| `events` | `GET /api/events` | Event types + listener counts |
| `services` | `GET /api/services` | Services by domain |
| `errors` | `GET /api/error_log` | Plaintext error log |
| `states` | `GET /api/states` | All entity states |
| `states <entity_id>` | `GET /api/states/<id>` | One entity, e.g. `states light.office` |
| `history <entity_id> [--start ISO] [--end ISO] [--minimal-response] [--no-attributes] [--significant-changes-only]` | `GET /api/history/period` | History for one entity |
| `logbook [--entity <id>] [--start ISO] [--end ISO]` | `GET /api/logbook` | Logbook entries |
| `calendars` | `GET /api/calendars` | List calendar entities |
| `calendar <entity_id> --start ISO --end ISO` | `GET /api/calendars/<id>` | Events on one calendar |
| `get <path> [--param KEY=VALUE ...]` | `GET <path>` | Escape hatch for any GET endpoint not covered above |

For the complete raw API reference — every endpoint (including the POST/DELETE
ones this skill doesn't wrap), status codes, and auth details — see
`references/api-reference.md`. Read it before reaching for `get <path>` on
something unfamiliar, or before writing a POST call by hand.

## Examples

```bash
# Is a light on right now?
python3 .claude/skills/ha-api-poll/scripts/poll.py states light.office

# What changed with a sensor in the last day?
python3 .claude/skills/ha-api-poll/scripts/poll.py history sensor.temperature

# Recent logbook activity for one entity
python3 .claude/skills/ha-api-poll/scripts/poll.py logbook --entity light.office

# Full config dump
python3 .claude/skills/ha-api-poll/scripts/poll.py config
```

## Notes

- Only GET (read) endpoints are covered — this skill is for polling/inspecting
  state, not for calling services or modifying entities. If the user wants to
  turn something on/off or call a service, use `get` cautiously or ask
  whether they want a separate action taken (that's a POST to
  `/api/services/<domain>/<service>`, not something to do silently while
  "just polling").
- If a call fails with 401, the token in `.env` may have expired — a new
  long-lived access token needs to be generated from the HA profile page.
- Timestamps for `history`/`logbook`/`calendar` are ISO 8601
  (e.g. `2026-07-31T00:00:00+00:00`). Omitting `--start` lets HA default to
  its own lookback window (usually the last day).
