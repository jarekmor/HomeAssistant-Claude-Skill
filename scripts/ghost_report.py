#!/usr/bin/env python3
"""Build the recorder ghost-entity report from live Home Assistant data.

A ghost is an entity_id that still has rows in the recorder database
(states_meta) but no longer exists in the entity registry (/api/states).
This script finds them, groups them, derives a purge plan that is verified
never to touch a live entity, and renders a self-contained HTML page.

Nothing about any particular installation is written into this file. Groups
and purge globs are discovered from the data itself, through three signals:

    death cohorts   entities deleted in one action stop reporting together
    affix families  a suffix or prefix repeated across many distinct names
    shared tokens   a name fragment common to many ghosts and few live entities

Two sources are read, both read-only:
    HA REST API   /api/states     -> the set of live entity_ids
    recorder DB   states_meta joined with states, via --db or sqlite_web

Credentials come from the environment (HA_URL, HA_TOKEN, and either
HA_RECORDER_DB or SQLITE_WEB_URL). If they aren't already set, a .env file
in the project root is loaded.

Only SQLite recorder databases are supported; MariaDB and PostgreSQL are
detected and refused rather than half-handled.

Usage examples:
    ghost_report.py --db ~/homeassistant/home-assistant_v2.db
    ghost_report.py --lang pl --out /tmp/report.html
    ghost_report.py --snapshot data.json             # also save raw data
    ghost_report.py --from-snapshot data.json        # rebuild, no network
    ghost_report.py --print-plan                     # purge YAML to stdout
"""
import argparse
import fnmatch
import html
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT / "reports" / "ghost-report.html"

# Only SELECT/PRAGMA statements are ever sent to the database.
SQL_ALLOWED = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

# Recorder backends this script deliberately does not handle.
UNSUPPORTED_DSN = re.compile(r"^(mysql|mariadb|postgres|postgresql)(\+[a-z0-9_]+)?://", re.I)

STATES_META_SQL = """
SELECT sm.entity_id AS entity_id,
       COUNT(s.state_id) AS rows_n,
       COALESCE(CAST(MAX(s.last_updated_ts) AS INT), 0) AS last_ts
FROM states_meta sm
LEFT JOIN states s ON s.metadata_id = sm.metadata_id
GROUP BY sm.metadata_id
ORDER BY sm.entity_id
"""

DB_INFO_SQL = """
SELECT (SELECT COUNT(*) FROM states) AS states_rows,
       (SELECT COUNT(*) FROM statistics) AS stats_rows,
       (SELECT COUNT(*) FROM statistics_meta) AS stats_meta_rows,
       page_count * page_size / 1024 / 1024 AS db_mb
FROM pragma_page_count(), pragma_page_size()
"""

# Thresholds for structure detection. They exist to keep coincidences out of
# the report: two entities sharing a suffix is noise, twenty is a pattern.
COHORT_GAP_SECONDS = 900     # a longer silence between deaths starts a cohort
MIN_COHORT = 3               # entities before a cohort is worth naming
MIN_FAMILY_ENTITIES = 4      # entities before an affix counts as a family
MIN_FAMILY_COUNTERPARTS = 3  # distinct names the affix must attach to
MIN_TOKEN_GHOSTS = 4         # ghosts before a shared token becomes a group
MIN_TOKEN_LEN = 4            # shorter fragments are too generic to mean anything
MAX_TOKEN_LIVE_SHARE = 0.2   # a token still common among live entities is not a mark of death
MIN_GLOB_GHOSTS = 2          # a glob covering a single ghost earns nothing over naming it


# --------------------------------------------------------------------------
# configuration and I/O
# --------------------------------------------------------------------------

def load_env_file():
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_config(db_path=None):
    """Resolve credentials. Exactly one database route has to be available."""
    load_env_file()
    ha_url = os.environ.get("HA_URL")
    token = os.environ.get("HA_TOKEN")
    db_path = db_path or os.environ.get("HA_RECORDER_DB")
    sqlite_url = os.environ.get("SQLITE_WEB_URL")

    missing = [name for name, value in (("HA_URL", ha_url), ("HA_TOKEN", token))
               if not value]
    if missing:
        sys.exit(f"Missing {', '.join(missing)}. Set them as environment "
                 f"variables or in {PROJECT_ROOT / '.env'}.")
    if not db_path and not sqlite_url:
        sys.exit(
            "No recorder database configured. Either pass "
            "--db /path/to/home-assistant_v2.db (or set HA_RECORDER_DB), or set "
            "SQLITE_WEB_URL to a sqlite_web instance serving that file.\n"
            "MariaDB and PostgreSQL recorders are not supported."
        )
    return ha_url.rstrip("/"), token, db_path, (sqlite_url or "").rstrip("/")


def fetch_live_entities(ha_url, token):
    """Return the set of entity_ids currently in the registry."""
    resp = requests.get(
        f"{ha_url}/api/states",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code == 401:
        sys.exit("401 Unauthorized — check HA_TOKEN is valid and not expired.")
    resp.raise_for_status()
    return {row["entity_id"] for row in resp.json()}


def sqlite_query(sqlite_url, sql, timeout=300):
    """Run one read-only query through sqlite_web's JSON export."""
    if not SQL_ALLOWED.match(sql):
        raise ValueError(f"Refusing non-SELECT statement: {sql.strip()[:60]}")
    resp = requests.post(
        f"{sqlite_url}/query/",
        data={"sql": sql, "export_json": "1"},
        timeout=timeout,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        sys.exit(
            f"sqlite_web did not return JSON for a query. Check that "
            f"{sqlite_url} serves the recorder database and needs no login."
        )


def sqlite_file_query(db_path, sql):
    """Run one read-only query against the recorder file directly.

    The connection is opened through a ``mode=ro`` URI, so the running
    recorder cannot be disturbed even if the guard above were bypassed.
    """
    if not SQL_ALLOWED.match(sql):
        raise ValueError(f"Refusing non-SELECT statement: {sql.strip()[:60]}")
    if UNSUPPORTED_DSN.match(str(db_path)):
        sys.exit(
            f"{db_path} looks like a MariaDB/PostgreSQL DSN. This script reads "
            "SQLite recorder files only — it cannot report on those backends."
        )
    path = Path(db_path).expanduser()
    if not path.exists():
        sys.exit(f"Recorder database not found: {path}")
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql)]
    except sqlite3.DatabaseError as exc:
        sys.exit(f"Cannot read {path} as a SQLite database: {exc}")
    finally:
        conn.close()


def make_query(db_path, sqlite_url):
    """Pick the database route and return (callable, source label)."""
    if db_path:
        path = Path(db_path).expanduser()
        return (lambda sql: sqlite_file_query(db_path, sql)), str(path)
    return (lambda sql: sqlite_query(sqlite_url, sql)), sqlite_url


def collect(ha_url, token, query, source):
    """Fetch everything the report needs and return it as one dict."""
    live = fetch_live_entities(ha_url, token)
    meta = query(STATES_META_SQL)
    db_info = query(DB_INFO_SQL)[0]
    return {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ha_url": ha_url,
        "source": source,
        "live": sorted(live),
        "meta": meta,
        "db_info": db_info,
    }


def snapshot_source(snapshot):
    """Where the data came from. ``sqlite_url`` is the pre-``--db`` key name."""
    return snapshot.get("source") or snapshot.get("sqlite_url", "")


# --------------------------------------------------------------------------
# structure detection — every grouping signal below reads only the data
# --------------------------------------------------------------------------

def object_id(entity_id):
    return entity_id.partition(".")[2] or entity_id


def name_tokens(entity_id):
    return [part for part in object_id(entity_id).split("_") if part]


def _affix_candidates(obj, kind):
    """Yield (affix, counterpart) for every underscore boundary in a name.

    ``grafana_cpu_usage`` offers the suffixes ``_usage`` and ``_cpu_usage``,
    each paired with the name that precedes it. Whole-name affixes are skipped:
    an affix with nothing on the other side describes one entity, not a shape.
    """
    parts = obj.split("_")
    for cut in range(1, len(parts)):
        if kind == "suffix":
            yield "_" + "_".join(parts[cut:]), "_".join(parts[:cut])
        else:
            yield "_".join(parts[:cut]) + "_", "_".join(parts[cut:])


def _collect_affixes(rows, kind):
    found = {}
    for row in rows:
        domain, _, obj = row["entity_id"].partition(".")
        for affix, counterpart in _affix_candidates(obj, kind):
            entry = found.setdefault((domain, affix, kind),
                                     {"ids": [], "rows": 0, "counterparts": set()})
            entry["ids"].append(row["entity_id"])
            entry["rows"] += row["rows_n"]
            entry["counterparts"].add(counterpart)
    return found


def _qualifies(entry):
    return (len(entry["ids"]) >= MIN_FAMILY_ENTITIES
            and len(entry["counterparts"]) >= MIN_FAMILY_COUNTERPARTS)


def affix_families(ghosts):
    """Detect repeated name shapes across the ghosts.

    ``_cpu_usage_total`` under twenty different prefixes is a family;
    ``_screen_state`` under one is not. Each entity is credited to its most
    specific qualifying family only, so the long form wins over the short one
    it contains and the table does not count the same entity twice.
    """
    rows_by_id = {row["entity_id"]: row for row in ghosts}
    candidates = {}
    for kind in ("suffix", "prefix"):
        candidates.update({k: v for k, v in _collect_affixes(ghosts, kind).items()
                           if _qualifies(v)})

    best = {}
    for key, entry in candidates.items():
        for entity_id in entry["ids"]:
            if entity_id not in best or len(key[1]) > len(best[entity_id][1]):
                best[entity_id] = key

    families = {}
    for entity_id, key in best.items():
        domain, affix, kind = key
        entry = families.setdefault(key, {"ids": [], "rows": 0, "counterparts": set()})
        entry["ids"].append(entity_id)
        entry["rows"] += rows_by_id[entity_id]["rows_n"]
        obj = object_id(entity_id)
        entry["counterparts"].add(obj[: -len(affix)] if kind == "suffix"
                                  else obj[len(affix):])
    return {key: entry for key, entry in families.items() if _qualifies(entry)}


def family_glob(key):
    """The fnmatch pattern a family describes, e.g. ``sensor.*_container``."""
    domain, affix, kind = key
    return f"{domain}.*{affix}" if kind == "suffix" else f"{domain}.{affix}*"


def family_tokens(families):
    """Name fragments a suffix family already explains, so tokens skip them.

    Only suffixes are excluded. A trailing ``_cpu_usage_total`` names a
    measurement and would make a meaningless group; a leading ``local_`` names
    the thing that was removed, which is exactly the label worth keeping.
    """
    return {part for _, affix, kind in families if kind == "suffix"
            for part in affix.split("_") if part}


def shared_tokens(ghosts, live, exclude=frozenset()):
    """Name fragments that mark a group of ghosts as one thing.

    A fragment carried by many ghosts and almost no live entity identifies
    something that was removed — a device, an integration, a host — without
    the script needing to know what it was.
    """
    live_counts = Counter()
    for entity_id in live:
        live_counts.update(set(name_tokens(entity_id)))

    by_token = {}
    for row in ghosts:
        for token in set(name_tokens(row["entity_id"])):
            by_token.setdefault(token, []).append(row)

    groups = []
    for token, rows in by_token.items():
        if (token in exclude or len(token) < MIN_TOKEN_LEN or token.isdigit()
                or len(rows) < MIN_TOKEN_GHOSTS):
            continue
        if live_counts[token] / (len(rows) + live_counts[token]) > MAX_TOKEN_LIVE_SHARE:
            continue
        groups.append((token, rows))
    return sorted(groups, key=lambda item: (-len(item[1]), item[0]))


def death_cohorts(rows, gap=COHORT_GAP_SECONDS):
    """Cluster entities by when they last wrote a row.

    Entities dropped by one deletion stop reporting within seconds of each
    other, so a long silence between two consecutive timestamps is a seam
    between two separate events. Returns (cohorts newest first, undated).
    """
    dated = sorted((r for r in rows if r.get("last_ts")), key=lambda r: r["last_ts"])
    undated = [r for r in rows if not r.get("last_ts")]
    cohorts = []
    for row in dated:
        if cohorts and row["last_ts"] - cohorts[-1][-1]["last_ts"] <= gap:
            cohorts[-1].append(row)
        else:
            cohorts.append([row])
    cohorts.reverse()
    return cohorts, undated


def time_span(members):
    stamps = [m["last_ts"] for m in members if m.get("last_ts")]
    return (min(stamps), max(stamps)) if stamps else None


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def find_ghosts(snapshot):
    """Entities with recorder rows but no registry entry."""
    live = set(snapshot["live"])
    return [row for row in snapshot["meta"] if row["entity_id"] not in live]


def group_ghosts(ghosts, live, families=None):
    """Partition the ghosts into report sections, most explicable first.

    Shared tokens run before cohorts because they carry a name: an entity
    belongs with the device it came from rather than with everything else
    deleted the same minute. Whatever no signal claims falls to the catch-all,
    and every ghost lands in exactly one group.
    """
    if families is None:
        families = affix_families(ghosts)

    groups, claimed = [], set()
    for token, rows in shared_tokens(ghosts, live, family_tokens(families)):
        members = [r for r in rows if r["entity_id"] not in claimed]
        if len(members) < MIN_TOKEN_GHOSTS:
            continue
        groups.append({"kind": "token", "key": token, "members": members,
                       "span": time_span(members)})
        claimed |= {m["entity_id"] for m in members}

    rest = [g for g in ghosts if g["entity_id"] not in claimed]
    cohorts, undated = death_cohorts(rest)
    leftover = []
    for cohort in cohorts:
        if len(cohort) >= MIN_COHORT:
            groups.append({"kind": "cohort", "key": None, "members": cohort,
                           "span": time_span(cohort)})
        else:
            leftover.extend(cohort)

    if leftover:
        groups.append({"kind": "other", "key": None,
                       "members": sorted(leftover, key=lambda r: r["entity_id"]),
                       "span": time_span(leftover)})
    if undated:
        groups.append({"kind": "norows", "key": None,
                       "members": sorted(undated, key=lambda r: r["entity_id"]),
                       "span": None})
    return groups


def candidate_globs(families, groups):
    """Derive purge globs from the detected structure.

    Nothing here is trusted: every pattern is re-checked against the live
    registry in build_purge_plan, and one live match is enough to drop it.
    """
    globs, seen = [], set()

    def add(glob, ghost_count):
        if ghost_count >= MIN_GLOB_GHOSTS and glob not in seen:
            seen.add(glob)
            globs.append(glob)

    for key, entry in sorted(families.items(), key=lambda kv: -len(kv[1]["ids"])):
        add(family_glob(key), len(entry["ids"]))
    for group in groups:
        if group["kind"] == "token":
            add(f'*{group["key"]}*', len(group["members"]))
    return globs


def build_purge_plan(ghosts, live, globs):
    """Split ghosts into glob-covered and explicit sets.

    A glob is only kept when it matches no live entity. Colliding globs are
    reported so the collision is visible, and their ghosts fall through to
    the explicit list -- the plan is safe by construction, not by review.
    """
    ghost_ids = {g["entity_id"] for g in ghosts}
    safe, collisions, covered = [], [], set()
    for glob in globs:
        hit_live = sorted(e for e in live if fnmatch.fnmatch(e, glob))
        hit_ghosts = {e for e in ghost_ids if fnmatch.fnmatch(e, glob)}
        if hit_live:
            collisions.append({"glob": glob, "live": hit_live, "ghosts": len(hit_ghosts)})
            continue
        if hit_ghosts:
            safe.append(glob)
            covered |= hit_ghosts
    return {
        "safe_globs": safe,
        "collisions": collisions,
        "covered": sorted(covered),
        "explicit": sorted(ghost_ids - covered),
    }


def analyse(snapshot):
    """Turn a raw snapshot into everything the template needs."""
    live = set(snapshot["live"])
    ghosts = find_ghosts(snapshot)
    meta_by_id = {r["entity_id"]: r for r in snapshot["meta"]}
    total_rows = sum(r["rows_n"] for r in snapshot["meta"])
    ghost_rows = sum(r["rows_n"] for r in ghosts)
    families = affix_families(ghosts)
    groups = group_ghosts(ghosts, live, families)
    top_live = sorted(
        (r for r in snapshot["meta"] if r["entity_id"] in live),
        key=lambda r: -r["rows_n"],
    )[:12]
    return {
        "snapshot": snapshot,
        "ghosts": ghosts,
        "meta_by_id": meta_by_id,
        "groups": groups,
        "families": families,
        "plan": build_purge_plan(ghosts, live, candidate_globs(families, groups)),
        "top_live": top_live,
        "totals": {
            "states_meta": len(snapshot["meta"]),
            "live": len(live),
            "ghosts": len(ghosts),
            "ghost_rows": ghost_rows,
            "total_rows": total_rows,
            "ghost_share": (ghost_rows / total_rows * 100) if total_rows else 0.0,
            "db_mb": snapshot["db_info"].get("db_mb", 0),
            "stats_meta": snapshot["db_info"].get("stats_meta_rows", 0),
        },
    }


def purge_yaml(plan, lang="en"):
    """Render the purge plan as titled, ready-to-paste Developer Tools actions.

    Returns a list of (title, yaml) pairs. Either half may be absent -- once
    the glob-covered entities are gone, only the explicit block remains.
    """
    s = STRINGS[lang]
    blocks = []
    if plan["safe_globs"]:
        globs = "\n".join(f'    - "{g}"' for g in plan["safe_globs"])
        blocks.append((
            s["block_globs"].format(n=len(plan["covered"])),
            "action: recorder.purge_entities\n"
            "data:\n  keep_days: 0\n  entity_globs:\n" + globs,
        ))
    if plan["explicit"]:
        ids = "\n".join(f"    - {e}" for e in plan["explicit"])
        blocks.append((
            s["block_explicit"].format(n=len(plan["explicit"])),
            "action: recorder.purge_entities\n"
            "data:\n  keep_days: 0\n  entity_id:\n" + ids,
        ))
    return blocks


# --------------------------------------------------------------------------
# report text
# --------------------------------------------------------------------------

STRINGS = {
    "en": {
        "title": "Recorder ghosts — a Home Assistant database audit",
        "eyebrow": "Home Assistant · recorder audit",
        "h1": "{n} ghosts in the database",
        "lede": "Entities that still have rows in the recorder database but no "
                "longer exist in the registry — with a purge plan checked "
                "against every live entity.",
        "src_source": "Source: <b>{v}</b>",
        "src_hidden": "Source: <b>the recorder database</b>",
        "src_db": "Database: <b>{v} MB</b>",
        "src_generated": "Generated <b>{v}</b>",
        "date_fmt": "%Y-%m-%d %H:%M",
        "stat_states_meta": "entities in <code>states_meta</code>",
        "stat_live": "live in the registry",
        "stat_ghosts": "ghosts",
        "stat_ghost_rows": "rows behind the ghosts",
        "stat_share": "of the database is ghosts",
        "sec_scale_h": "Scale",
        "sec_scale_p": "How much of the database the ghosts hold, and how much "
                       "your own working entities do.",
        "note_share_low_h": "Purging ghosts will not shrink the database",
        "note_share_high_h": "Ghosts hold a real share of the database",
        "note_share_p": "The ghosts account for <b>{rows} rows out of {total}</b>, "
                        "or <b>{pct}</b> of the <code>states</code> table. ",
        "note_share_low_tail": "Deleting them will barely change the file size — "
                               "the leverage is in the table below.",
        "note_share_high_tail": "Cleaning up here is worth doing.",
        "note_stats_h": "Long-term statistics",
        "note_stats_p": "<code>statistics_meta</code> holds {n} entries.",
        "sec_top_h": "What weighs on the database",
        "sec_top_p": "The twelve live entities with the most rows.",
        "th_entity": "Entity",
        "th_rows": "Rows",
        "th_share": "Share",
        "top_hint": "If you want a smaller database, these are the targets: "
                    "<code>recorder: exclude</code>, a longer <code>scan_interval</code> "
                    "on chatty sensors, or a shorter <code>purge_keep_days</code>.",
        "sec_fam_h": "Recurring name patterns — {n} entities",
        "sec_fam_p": "Shapes repeated across many different names, usually one "
                     "entity set created per device or container. <code>*</code> "
                     "stands for the part that varies.",
        "th_pattern": "Pattern",
        "th_entities": "Entities",
        "th_names": "Distinct names",
        "total": "Total",
        "sec_groups_h": "Ghosts by origin",
        "sec_groups_p": "Groups are derived from the data: entities that went "
                        "silent together, or that share a fragment of their name. "
                        "The number beside an entity is its row count in "
                        "<code>states</code>.",
        "grp_meta": "<b>{n}</b> entities · {rows} rows",
        "grp_token": "Shared name fragment: {key}",
        "grp_cohort_at": "Went silent {when}",
        "grp_cohort_between": "Went silent between {start} and {end}",
        "grp_other": "Everything else",
        "grp_norows": "No rows in <code>states</code>",
        "note_cohort_tight": "All of these stopped writing within "
                             "{minutes} minutes of each other — the mark of a "
                             "single removal.",
        "note_other": "Too few, too scattered in time, and with no shared name "
                      "fragment — nothing here suggests a common cause.",
        "note_norows": "Present in <code>states_meta</code> but with no rows "
                       "left in <code>states</code>; purging them frees no space.",
        "sec_plan_h": "Purge plan",
        "sec_plan_p": "Every glob is checked against all {n} entities in "
                      "<code>states_meta</code>. A glob that matches a live entity "
                      "is dropped automatically and its ghosts move to the "
                      "explicit list.",
        "note_collisions_h": "Globs dropped automatically",
        "note_collisions_p": "These patterns reached entities still present in the "
                             "registry, so the script did not use them — their "
                             "ghosts went to the explicit list instead.",
        "collision_item": "would have covered {n} ghosts, but also matches live:",
        "block_globs": "Safe globs · {n} entities",
        "block_explicit": "Explicit list · {n} entities",
        "note_nothing_h": "Nothing to clean up",
        "note_nothing_p": "Every <code>entity_id</code> in the database has a "
                          "counterpart in the registry.",
        "note_after_h": "After the purge",
        "note_after_p": "SQLite does not return space to the filesystem without "
                        "<code>VACUUM</code> — the recorder does that on "
                        "<code>recorder.purge</code> with <code>repack: true</code>, "
                        "at the cost of locking the database for a few minutes.",
        "sec_method_h": "How this was measured",
        "method_def": "A <b>ghost</b> is an <code>entity_id</code> present in "
                      "<code>states_meta</code> and absent from the "
                      "<code>/api/states</code> response.",
        "method_sums": "Checksum: {covered} covered by globs + {explicit} listed "
                       "explicitly = <b>{total}</b>.",
        "method_readonly": "<b>Only SELECT queries</b> were run against the database.",
        "code_query": "Source query",
        "copy": "copy",
        "copied": "copied",
        "copy_manual": "select manually",
        "foot": "Generated by scripts/ghost_report.py · {when}",
    },
    "pl": {
        "title": "Duchy recordera — audyt bazy Home Assistant",
        "eyebrow": "Home Assistant · audyt recordera",
        "h1": "{n} duchów w bazie",
        "lede": "Encje, które mają wiersze w bazie recordera, ale nie istnieją "
                "już w rejestrze — wraz z planem czyszczenia zweryfikowanym "
                "względem żywych encji.",
        "src_source": "Źródło: <b>{v}</b>",
        "src_hidden": "Źródło: <b>baza recordera</b>",
        "src_db": "Baza: <b>{v} MB</b>",
        "src_generated": "Wygenerowano <b>{v}</b>",
        "date_fmt": "%d.%m.%Y %H:%M",
        "stat_states_meta": "encji w <code>states_meta</code>",
        "stat_live": "żywych w rejestrze",
        "stat_ghosts": "duchów",
        "stat_ghost_rows": "wierszy duchów",
        "stat_share": "udział duchów w bazie",
        "sec_scale_h": "Skala",
        "sec_scale_p": "Ile z bazy zajmują duchy, a ile Twoje własne, działające encje.",
        "note_share_low_h": "Purga duchów nie odchudzi bazy",
        "note_share_high_h": "Duchy zajmują istotną część bazy",
        "note_share_p": "Duchy to <b>{rows} wierszy z {total}</b>, czyli "
                        "<b>{pct}</b> tabeli <code>states</code>. ",
        "note_share_low_tail": "Usunięcie ich nie zmieni w praktyce rozmiaru "
                               "pliku — dźwignia leży w tabeli niżej.",
        "note_share_high_tail": "Czyszczenie ma tu realny sens.",
        "note_stats_h": "Statystyki długoterminowe",
        "note_stats_p": "<code>statistics_meta</code> ma {n} pozycji.",
        "sec_top_h": "Co najbardziej obciąża bazę",
        "sec_top_p": "Dwanaście żywych encji z największą liczbą wierszy.",
        "th_entity": "Encja",
        "th_rows": "Wierszy",
        "th_share": "Udział",
        "top_hint": "Jeśli chcesz zmniejszyć bazę, to są cele: "
                    "<code>recorder: exclude</code>, dłuższy <code>scan_interval</code> "
                    "na czujnikach albo krótszy <code>purge_keep_days</code>.",
        "sec_fam_h": "Powtarzalne wzorce nazw — {n} encji",
        "sec_fam_p": "Kształty powtórzone przy wielu różnych nazwach, zwykle "
                     "komplet encji tworzony na każde urządzenie albo kontener. "
                     "<code>*</code> oznacza część zmienną.",
        "th_pattern": "Wzorzec",
        "th_entities": "Encji",
        "th_names": "Różnych nazw",
        "total": "Razem",
        "sec_groups_h": "Duchy według pochodzenia",
        "sec_groups_p": "Grupy wynikają z danych: encje, które zamilkły razem, "
                        "albo mają wspólny człon nazwy. Liczba przy encji to jej "
                        "wiersze w tabeli <code>states</code>.",
        "grp_meta": "<b>{n}</b> encji · {rows} wierszy",
        "grp_token": "Wspólny człon nazwy: {key}",
        "grp_cohort_at": "Zamilkły {when}",
        "grp_cohort_between": "Zamilkły między {start} a {end}",
        "grp_other": "Pozostałe",
        "grp_norows": "Bez wierszy w <code>states</code>",
        "note_cohort_tight": "Wszystkie przestały pisać w odstępie {minutes} minut "
                             "— to ślad jednego usunięcia.",
        "note_other": "Za mało ich, zbyt rozrzucone w czasie i bez wspólnego "
                      "członu nazwy — nic tu nie wskazuje na wspólną przyczynę.",
        "note_norows": "Są w <code>states_meta</code>, ale nie zostało po nich "
                       "ani jednego wiersza w <code>states</code>; ich purga nie "
                       "zwolni miejsca.",
        "sec_plan_h": "Plan czyszczenia",
        "sec_plan_p": "Każdy glob sprawdzony względem wszystkich {n} encji z "
                      "<code>states_meta</code>. Glob trafiający w żywą encję jest "
                      "odrzucany automatycznie, a jego duchy trafiają do listy jawnej.",
        "note_collisions_h": "Globy odrzucone automatycznie",
        "note_collisions_p": "Te wzorce trafiały w encje wciąż obecne w rejestrze, "
                             "więc skrypt ich nie użył — ich duchy trafiły do "
                             "listy jawnej.",
        "collision_item": "pokryłby {n} duchów, ale trafia też w żywe:",
        "block_globs": "Bezpieczne globy · {n} encji",
        "block_explicit": "Lista jawna · {n} encji",
        "note_nothing_h": "Nie ma czego czyścić",
        "note_nothing_p": "Każdy <code>entity_id</code> w bazie ma odpowiednik "
                          "w rejestrze.",
        "note_after_h": "Po purdze",
        "note_after_p": "SQLite nie zwraca miejsca systemowi bez <code>VACUUM</code> "
                        "— recorder robi to przy <code>recorder.purge</code> z "
                        "<code>repack: true</code>, kosztem kilkuminutowej blokady bazy.",
        "sec_method_h": "Jak to zmierzono",
        "method_def": "<b>Duch</b> = <code>entity_id</code> obecny w "
                      "<code>states_meta</code>, nieobecny w odpowiedzi "
                      "<code>/api/states</code>.",
        "method_sums": "Sumy kontrolne: {covered} pokrytych globami + {explicit} "
                       "z listy jawnej = <b>{total}</b>.",
        "method_readonly": "Na bazie wykonano <b>wyłącznie zapytania SELECT</b>.",
        "code_query": "Zapytanie źródłowe",
        "copy": "kopiuj",
        "copied": "skopiowane",
        "copy_manual": "zaznacz recznie",
        "foot": "Wygenerowane przez scripts/ghost_report.py · {when}",
    },
}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def fmt(n, lang="en"):
    """Thousands separator: a non-breaking space in Polish, a comma in English."""
    return f"{n:,}".replace(",", " ") if lang == "pl" else f"{n:,}"


def pct(value, lang="en"):
    text = f"{value:.1f}"
    return f"{text.replace('.', ',')} %" if lang == "pl" else f"{text}%"


def stamp(ts, lang="en"):
    return datetime.fromtimestamp(ts).astimezone().strftime(STRINGS[lang]["date_fmt"])


def esc(text):
    return html.escape(str(text))


CSS = """
:root {
  --ground:#eef1f3; --surface:#fbfcfd; --surface-2:#e6eaed;
  --ink:#141b21; --ink-2:#4d5b66; --ink-3:#78868f;
  --rule:#cfd7dc; --rule-2:#dde3e7;
  --accent:#0c5b62; --accent-soft:#d3e5e6;
  --crit:#8c2f27; --crit-soft:#f2ddda;
  --warn:#7d5504; --warn-soft:#f4e6c9;
  --ok:#2a5d43; --ok-soft:#d9e8de;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,"Cascadia Mono","Roboto Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#0d1216; --surface:#141b21; --surface-2:#1b242b;
    --ink:#e3e9ed; --ink-2:#9aa9b3; --ink-3:#71818c;
    --rule:#28333b; --rule-2:#202a31;
    --accent:#59b3b8; --accent-soft:#16333a;
    --crit:#e5867c; --crit-soft:#3a201d;
    --warn:#d9a642; --warn-soft:#382c14;
    --ok:#77b795; --ok-soft:#1a2f25;
  }
}
:root[data-theme="dark"] {
  --ground:#0d1216; --surface:#141b21; --surface-2:#1b242b;
  --ink:#e3e9ed; --ink-2:#9aa9b3; --ink-3:#71818c;
  --rule:#28333b; --rule-2:#202a31;
  --accent:#59b3b8; --accent-soft:#16333a;
  --crit:#e5867c; --crit-soft:#3a201d;
  --warn:#d9a642; --warn-soft:#382c14;
  --ok:#77b795; --ok-soft:#1a2f25;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--ground); color:var(--ink);
  font-family:var(--sans); font-size:16px; line-height:1.65;
  -webkit-font-smoothing:antialiased; }
.wrap { max-width:1080px; margin:0 auto;
  padding:clamp(1.5rem,4vw,3.5rem) clamp(1rem,4vw,2.5rem) 5rem; }
.prose { max-width:68ch; color:var(--ink-2); font-size:.93rem; }
h1,h2,h3 { font-family:var(--mono); font-weight:650; text-wrap:balance;
  letter-spacing:-.02em; margin:0; }
h1 { font-size:clamp(1.75rem,4.5vw,2.6rem); line-height:1.15; }
h2 { font-size:1.3rem; letter-spacing:-.01em; }
h3 { font-size:1rem; letter-spacing:0; }
p { margin:0; }
code { font-family:var(--mono); font-size:.875em; }
.head { display:flex; flex-direction:column; gap:1.1rem; padding-bottom:2rem;
  border-bottom:2px solid var(--ink); }
.eyebrow { font-family:var(--mono); font-size:.7rem; letter-spacing:.16em;
  text-transform:uppercase; color:var(--accent); font-weight:600; }
.lede { font-size:1.1rem; color:var(--ink-2); max-width:60ch; }
.src { display:flex; flex-wrap:wrap; gap:.4rem 1.5rem; font-family:var(--mono);
  font-size:.76rem; color:var(--ink-3); }
.src b { color:var(--ink-2); font-weight:600; }
.stats { display:grid; gap:1px; background:var(--rule);
  grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  border:1px solid var(--rule); margin:2.5rem 0; }
.stat { background:var(--surface); padding:1.1rem 1.2rem; }
.stat__v { font-family:var(--mono); font-size:1.6rem; font-weight:650;
  font-variant-numeric:tabular-nums; letter-spacing:-.03em; line-height:1.1; }
.stat__l { font-size:.78rem; color:var(--ink-2); margin-top:.25rem; }
.stat--crit .stat__v { color:var(--crit); }
.stat--ok .stat__v { color:var(--ok); }
section { margin-top:3.5rem; display:flex; flex-direction:column; gap:1.1rem; }
.sec-head { display:flex; flex-direction:column; gap:.4rem;
  padding-left:.9rem; border-left:3px solid var(--accent); }
.sec-head p { color:var(--ink-2); max-width:66ch; }
.note { padding:1rem 1.15rem; border:1px solid var(--rule); background:var(--surface);
  border-left-width:4px; display:flex; flex-direction:column; gap:.5rem; }
.note--crit { border-left-color:var(--crit); background:var(--crit-soft); }
.note--warn { border-left-color:var(--warn); background:var(--warn-soft); }
.note--ok { border-left-color:var(--ok); background:var(--ok-soft); }
.note h3 { font-size:.82rem; letter-spacing:.06em; text-transform:uppercase; }
.note--crit h3 { color:var(--crit); }
.note--warn h3 { color:var(--warn); }
.note--ok h3 { color:var(--ok); }
.note p { font-size:.93rem; }
.scroll { overflow-x:auto; border:1px solid var(--rule); background:var(--surface); }
table { border-collapse:collapse; width:100%; font-size:.86rem; }
th,td { text-align:left; padding:.55rem .85rem; border-bottom:1px solid var(--rule-2); }
thead th { font-family:var(--mono); font-size:.7rem; letter-spacing:.09em;
  text-transform:uppercase; color:var(--ink-2); border-bottom:1px solid var(--rule);
  background:var(--surface-2); white-space:nowrap; font-weight:600; }
tbody tr:last-child td { border-bottom:none; }
tfoot td { font-family:var(--mono); font-weight:650; border-top:1px solid var(--rule);
  background:var(--surface-2); }
.num { text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums;
  white-space:nowrap; }
td.bar { width:34%; min-width:110px; }
td.bar span { display:block; height:7px; background:var(--accent); opacity:.65; }
.grp { border:1px solid var(--rule); background:var(--surface); }
.grp + .grp { margin-top:.6rem; }
.grp summary { cursor:pointer; padding:.8rem 1rem; display:flex; flex-wrap:wrap;
  gap:.3rem 1rem; align-items:baseline; justify-content:space-between; list-style:none; }
.grp summary::-webkit-details-marker { display:none; }
.grp summary:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
.grp__t { font-family:var(--mono); font-size:.92rem; font-weight:650; }
.grp__t::before { content:"\\25b8"; color:var(--accent); display:inline-block;
  width:1.1em; transition:transform .15s ease; }
.grp[open] .grp__t::before { transform:rotate(90deg); }
.grp__m { font-family:var(--mono); font-size:.74rem; color:var(--ink-2);
  font-variant-numeric:tabular-nums; }
.grp__note { margin:0 1rem .8rem; padding-left:1.1em; font-size:.88rem;
  color:var(--ink-2); max-width:70ch; }
.chips { list-style:none; margin:0; padding:0 1rem 1rem 2.1rem;
  display:grid; gap:1px 1.5rem;
  grid-template-columns:repeat(auto-fill,minmax(min(100%,330px),1fr)); }
.chip { display:flex; gap:.6rem; justify-content:space-between; align-items:baseline;
  padding:.16rem 0; border-bottom:1px dotted var(--rule); }
.chip code { font-size:.775rem; word-break:break-all; }
.chip__n { font-family:var(--mono); font-size:.7rem; color:var(--ink-3);
  font-variant-numeric:tabular-nums; white-space:nowrap; }
.code { border:1px solid var(--rule); background:var(--surface); }
.code__bar { display:flex; justify-content:space-between; align-items:center;
  gap:1rem; padding:.45rem .5rem .45rem .85rem; border-bottom:1px solid var(--rule);
  background:var(--surface-2); }
.code__t { font-family:var(--mono); font-size:.72rem; letter-spacing:.06em;
  text-transform:uppercase; color:var(--ink-2); font-weight:600; }
.code pre { margin:0; padding:.9rem 1rem; overflow-x:auto; }
.code pre code { font-size:.79rem; line-height:1.6; }
button.copy { font-family:var(--mono); font-size:.7rem; letter-spacing:.04em;
  padding:.28rem .6rem; cursor:pointer; color:var(--ink-2);
  background:var(--surface); border:1px solid var(--rule); }
button.copy:hover { color:var(--accent); border-color:var(--accent); }
button.copy:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
button.copy[data-done="1"] { color:var(--ok); border-color:var(--ok); }
ul.plain { margin:0; padding-left:1.15rem; display:flex; flex-direction:column; gap:.4rem; }
ul.plain li { color:var(--ink-2); }
ul.plain b, .prose b { color:var(--ink); font-weight:650; }
.foot { margin-top:4rem; padding-top:1.2rem; border-top:1px solid var(--rule);
  font-family:var(--mono); font-size:.72rem; color:var(--ink-3); }
@media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
"""

COPY_JS = """
document.querySelectorAll("button.copy").forEach(function (b) {
  b.addEventListener("click", function () {
    var pre = b.closest(".code").querySelector("pre");
    var txt = pre ? pre.innerText : "";
    var done = function () {
      b.textContent = %(copied)s; b.dataset.done = "1";
      setTimeout(function () { b.textContent = %(copy)s; b.dataset.done = ""; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(done, function () {
        b.textContent = %(manual)s;
      });
    } else {
      var t = document.createElement("textarea");
      t.value = txt; document.body.appendChild(t); t.select();
      try { document.execCommand("copy"); done(); }
      catch (e) { b.textContent = %(manual)s; }
      document.body.removeChild(t);
    }
  });
});
"""


def group_label(group, lang):
    """The heading for a group, built from the signal that produced it."""
    s = STRINGS[lang]
    if group["kind"] == "token":
        return s["grp_token"].format(key=f"<code>{esc(group['key'])}</code>")
    if group["kind"] == "cohort":
        start, end = group["span"]
        if end - start <= COHORT_GAP_SECONDS:
            return s["grp_cohort_at"].format(when=stamp(end, lang))
        return s["grp_cohort_between"].format(start=stamp(start, lang),
                                              end=stamp(end, lang))
    return s["grp_norows"] if group["kind"] == "norows" else s["grp_other"]


def group_note(group, lang):
    s = STRINGS[lang]
    if group["kind"] == "norows":
        return s["note_norows"]
    if group["kind"] == "other":
        return s["note_other"]
    if group["kind"] == "token" and group["span"]:
        start, end = group["span"]
        if end - start <= COHORT_GAP_SECONDS:
            return s["note_cohort_tight"].format(minutes=max(1, round((end - start) / 60)))
    return ""


def render_chips(ids, meta_by_id, lang):
    items = []
    for entity_id in sorted(ids):
        rows = meta_by_id.get(entity_id, {}).get("rows_n", 0)
        items.append(
            f'<li class="chip"><code>{esc(entity_id)}</code>'
            f'<span class="chip__n">{fmt(rows, lang)}</span></li>'
        )
    return '<ul class="chips">' + "".join(items) + "</ul>"


def render_group(label, ids, meta_by_id, lang, note="", open_=False):
    rows = sum(meta_by_id.get(i, {}).get("rows_n", 0) for i in ids)
    meta = STRINGS[lang]["grp_meta"].format(n=len(ids), rows=fmt(rows, lang))
    return (
        f'<details class="grp"{" open" if open_ else ""}>'
        f'<summary><span class="grp__t">{label}</span>'
        f'<span class="grp__m">{meta}</span>'
        f"</summary>"
        + (f'<p class="grp__note">{note}</p>' if note else "")
        + render_chips(ids, meta_by_id, lang)
        + "</details>"
    )


def render_code(title, body, lang):
    return (
        f'<div class="code"><div class="code__bar">'
        f'<span class="code__t">{esc(title)}</span>'
        f'<button class="copy" type="button">{esc(STRINGS[lang]["copy"])}</button></div>'
        f"<pre><code>{esc(body)}</code></pre></div>"
    )


def render_section(heading, intro, body):
    """One report section. Callers pass an empty body to omit it entirely."""
    if not body:
        return ""
    return (f'<section><div class="sec-head"><h2>{heading}</h2>'
            + (f"<p>{intro}</p>" if intro else "")
            + f"</div>{body}</section>")


def render(report, lang="en", hide_source=False):
    """Render the page. ``hide_source`` drops the database address from the
    header — the report still names every entity, but a page meant to be
    shown to someone else should not also hand over the host it came from."""
    s = STRINGS[lang]
    t = report["totals"]
    meta_by_id = report["meta_by_id"]
    plan = report["plan"]
    generated = datetime.fromisoformat(report["snapshot"]["generated"])

    stats = [
        (fmt(t["states_meta"], lang), s["stat_states_meta"], ""),
        (fmt(t["live"], lang), s["stat_live"], ""),
        (fmt(t["ghosts"], lang), s["stat_ghosts"], "crit"),
        (fmt(t["ghost_rows"], lang), s["stat_ghost_rows"], ""),
        (pct(t["ghost_share"], lang), s["stat_share"], "ok"),
    ]
    stat_html = "".join(
        f'<div class="stat{" stat--" + c if c else ""}">'
        f'<div class="stat__v">{v}</div><div class="stat__l">{label}</div></div>'
        for v, label, c in stats
    )

    # --- scale -------------------------------------------------------------
    low = t["ghost_share"] < 10
    scale_body = (
        f'<div class="note note--{"crit" if low else "warn"}">'
        f'<h3>{s["note_share_low_h"] if low else s["note_share_high_h"]}</h3>'
        f'<p>{s["note_share_p"].format(rows=fmt(t["ghost_rows"], lang), total=fmt(t["total_rows"], lang), pct=pct(t["ghost_share"], lang))}'
        f'{s["note_share_low_tail"] if low else s["note_share_high_tail"]}</p></div>'
        f'<div class="note note--{"ok" if t["stats_meta"] else "warn"}">'
        f'<h3>{s["note_stats_h"]}</h3>'
        f'<p>{s["note_stats_p"].format(n=fmt(t["stats_meta"], lang))}</p></div>'
    )

    # --- heaviest live entities -------------------------------------------
    top = report["top_live"]
    top_body = ""
    if top:
        peak = top[0]["rows_n"] or 1
        top_rows = "".join(
            f'<tr><td><code>{esc(r["entity_id"])}</code></td>'
            f'<td class="num">{fmt(r["rows_n"], lang)}</td>'
            f'<td class="bar"><span style="width:{r["rows_n"] / peak * 100:.1f}%"></span></td></tr>'
            for r in top
        )
        top_body = (
            '<div class="scroll"><table><thead><tr>'
            f'<th>{s["th_entity"]}</th><th class="num">{s["th_rows"]}</th>'
            f'<th>{s["th_share"]}</th></tr></thead>'
            f'<tbody>{top_rows}</tbody></table></div>'
            f'<p class="prose">{s["top_hint"]}</p>'
        )

    # --- recurring name patterns ------------------------------------------
    families = sorted(report["families"].items(), key=lambda kv: -len(kv[1]["ids"]))
    fam_n = sum(len(entry["ids"]) for _, entry in families)
    fam_body = ""
    if families:
        fam_rows = "".join(
            f'<tr><td><code>{esc(family_glob(key))}</code></td>'
            f'<td class="num">{len(entry["ids"])}</td>'
            f'<td class="num">{len(entry["counterparts"])}</td>'
            f'<td class="num">{fmt(entry["rows"], lang)}</td></tr>'
            for key, entry in families
        )
        fam_total_rows = sum(entry["rows"] for _, entry in families)
        fam_body = (
            '<div class="scroll"><table><thead><tr>'
            f'<th>{s["th_pattern"]}</th><th class="num">{s["th_entities"]}</th>'
            f'<th class="num">{s["th_names"]}</th><th class="num">{s["th_rows"]}</th>'
            f'</tr></thead><tbody>{fam_rows}</tbody>'
            f'<tfoot><tr><td>{s["total"]}</td><td class="num">{fam_n}</td>'
            f'<td class="num"></td><td class="num">{fmt(fam_total_rows, lang)}</td>'
            "</tr></tfoot></table></div>"
        )

    # --- groups ------------------------------------------------------------
    groups_body = "\n".join(
        render_group(group_label(g, lang), [m["entity_id"] for m in g["members"]],
                     meta_by_id, lang, group_note(g, lang), open_=(i == 0))
        for i, g in enumerate(report["groups"])
    )

    # --- purge plan --------------------------------------------------------
    code_html = "".join(render_code(title, body, lang)
                        for title, body in purge_yaml(plan, lang))
    if not code_html:
        code_html = (f'<div class="note note--ok"><h3>{s["note_nothing_h"]}</h3>'
                     f'<p>{s["note_nothing_p"]}</p></div>')

    collisions_html = ""
    if plan["collisions"]:
        rows = "".join(
            f'<li><code>{esc(c["glob"])}</code> — '
            + s["collision_item"].format(n=c["ghosts"]) + " "
            + ", ".join(f"<code>{esc(e)}</code>" for e in c["live"]) + "</li>"
            for c in plan["collisions"]
        )
        collisions_html = (
            f'<div class="note note--warn"><h3>{s["note_collisions_h"]}</h3>'
            f'<p>{s["note_collisions_p"]}</p>'
            f'<ul class="plain">{rows}</ul></div>'
        )

    plan_body = (
        collisions_html + code_html
        + f'<div class="note"><h3>{s["note_after_h"]}</h3>'
        f'<p>{s["note_after_p"]}</p></div>'
    )

    method_body = (
        '<ul class="plain">'
        f'<li>{s["method_def"]}</li>'
        f'<li>{s["method_sums"].format(covered=fmt(len(plan["covered"]), lang), explicit=fmt(len(plan["explicit"]), lang), total=fmt(t["ghosts"], lang))}</li>'
        f'<li>{s["method_readonly"]}</li></ul>'
        + render_code(s["code_query"], STATES_META_SQL.strip(), lang)
    )

    copy_js = COPY_JS % {"copied": json.dumps(s["copied"]),
                         "copy": json.dumps(s["copy"]),
                         "manual": json.dumps(s["copy_manual"])}

    return f"""<title>{esc(s["title"])}</title>
<style>{CSS}</style>
<div class="wrap">

<header class="head">
  <div class="eyebrow">{s["eyebrow"]}</div>
  <h1>{s["h1"].format(n=fmt(t["ghosts"], lang))}</h1>
  <p class="lede">{s["lede"]}</p>
  <div class="src">
    <span>{s["src_hidden"] if hide_source else s["src_source"].format(v=esc(snapshot_source(report["snapshot"])))}</span>
    <span>{s["src_db"].format(v=fmt(t["db_mb"], lang))}</span>
    <span>{s["src_generated"].format(v=generated.strftime(s["date_fmt"]))}</span>
  </div>
</header>

<div class="stats">{stat_html}</div>
{render_section(s["sec_scale_h"], s["sec_scale_p"], scale_body)}
{render_section(s["sec_top_h"], s["sec_top_p"], top_body)}
{render_section(s["sec_fam_h"].format(n=fam_n), s["sec_fam_p"], fam_body)}
{render_section(s["sec_groups_h"], s["sec_groups_p"], groups_body)}
{render_section(s["sec_plan_h"], s["sec_plan_p"].format(n=fmt(t["states_meta"], lang)), plan_body)}
{render_section(s["sec_method_h"], "", method_body)}

<p class="foot">{s["foot"].format(when=esc(report["snapshot"]["generated"]))}</p>

</div>
<script>{copy_js}</script>
"""


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build the recorder ghost-entity report as a standalone HTML page.")
    parser.add_argument("--db", type=str,
                        help="path to home-assistant_v2.db; read directly, read-only")
    parser.add_argument("--lang", choices=sorted(STRINGS), default="en",
                        help="report language (default: en)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output HTML path (default: {DEFAULT_OUT})")
    parser.add_argument("--snapshot", type=Path,
                        help="also write the raw fetched data as JSON")
    parser.add_argument("--from-snapshot", type=Path,
                        help="rebuild from a saved snapshot, no network access")
    parser.add_argument("--print-plan", action="store_true",
                        help="print the purge YAML to stdout as well")
    parser.add_argument("--hide-source", action="store_true",
                        help="keep the database address out of the page header")
    parser.add_argument("--quiet", action="store_true", help="suppress the summary")
    args = parser.parse_args()

    if args.from_snapshot:
        snapshot = json.loads(args.from_snapshot.read_text())
    else:
        ha_url, token, db_path, sqlite_url = get_config(args.db)
        query, source = make_query(db_path, sqlite_url)
        snapshot = collect(ha_url, token, query, source)
        if args.snapshot:
            args.snapshot.parent.mkdir(parents=True, exist_ok=True)
            args.snapshot.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")

    report = analyse(snapshot)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(report, args.lang, args.hide_source), encoding="utf-8")

    if args.print_plan:
        for title, block in purge_yaml(report["plan"], args.lang):
            print(f"# {title}\n{block}\n")

    if not args.quiet:
        t = report["totals"]
        plan = report["plan"]
        print(f"states_meta {t['states_meta']} | live {t['live']} | ghosts {t['ghosts']}")
        print(f"ghost rows  {t['ghost_rows']} of {t['total_rows']} "
              f"({t['ghost_share']:.1f}% of states)")
        print(f"groups      {len(report['groups'])} from "
              f"{len(report['families'])} name patterns")
        print(f"purge plan  {len(plan['covered'])} via {len(plan['safe_globs'])} globs "
              f"+ {len(plan['explicit'])} explicit")
        for collision in plan["collisions"]:
            print(f"  dropped glob {collision['glob']!r}: matches live "
                  f"{', '.join(collision['live'])}")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
