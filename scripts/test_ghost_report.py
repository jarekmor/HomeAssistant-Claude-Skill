"""Unit tests for ghost_report.py.

Covers ghost detection, purge-plan safety, the data-driven grouping signals
and rendering with synthetic data — no Home Assistant instance or database is
contacted. One test replays the real snapshot in reports/ when it is present.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent / "ghost_report.py"
spec = importlib.util.spec_from_file_location("ghost_report", SCRIPT_PATH)
ghost_report = importlib.util.module_from_spec(spec)
sys.modules["ghost_report"] = ghost_report
spec.loader.exec_module(ghost_report)

SNAPSHOT_PATH = SCRIPT_PATH.parents[1] / "reports" / "snapshot.json"

# 2026-08-08 09:48 local-ish; the exact instant does not matter, only the gaps.
T0 = 1786000000


def meta_row(entity_id, rows_n=10, last_ts=T0):
    return {"entity_id": entity_id, "rows_n": rows_n, "last_ts": last_ts}


def make_snapshot(live, meta_ids):
    return {
        "generated": "2026-08-08T12:55:00+02:00",
        "ha_url": "http://ha.local:8123",
        "source": "/data/home-assistant_v2.db",
        "live": sorted(live),
        "meta": [meta_row(e) for e in meta_ids],
        "db_info": {"states_rows": 100, "stats_rows": 5,
                    "stats_meta_rows": 3, "db_mb": 42},
    }


def containers(suffix, names=("grafana", "traefik", "mosquitto", "zigbee2mqtt", "esphome")):
    return [meta_row(f"sensor.{n}{suffix}") for n in names]


class TestFindGhosts:
    def test_returns_entities_absent_from_registry(self):
        snap = make_snapshot(
            live=["light.kitchen"],
            meta_ids=["light.kitchen", "switch.old_container"],
        )
        assert [g["entity_id"] for g in ghost_report.find_ghosts(snap)] == [
            "switch.old_container"
        ]

    def test_empty_when_registry_covers_everything(self):
        snap = make_snapshot(live=["light.kitchen"], meta_ids=["light.kitchen"])
        assert ghost_report.find_ghosts(snap) == []


class TestBuildPurgePlan:
    """The safety property. Globs are supplied explicitly here so these
    assertions test the verifier, not whatever the detector happened to emit."""

    def test_glob_is_used_when_it_matches_no_live_entity(self):
        ghosts = [meta_row("switch.grafana_container"),
                  meta_row("switch.traefik_container")]
        plan = ghost_report.build_purge_plan(ghosts, live={"light.kitchen"},
                                             globs=["*_container"])
        assert "*_container" in plan["safe_globs"]
        assert len(plan["covered"]) == 2
        assert plan["explicit"] == []

    def test_colliding_glob_is_dropped_and_ghosts_go_explicit(self):
        ghosts = [meta_row("sensor.grafana_memory_usage")]
        live = {"sensor.system_monitor_memory_usage"}
        plan = ghost_report.build_purge_plan(ghosts, live,
                                             globs=["sensor.*_memory_usage"])
        assert "sensor.*_memory_usage" not in plan["safe_globs"]
        assert plan["explicit"] == ["sensor.grafana_memory_usage"]
        assert plan["collisions"][0]["glob"] == "sensor.*_memory_usage"
        assert plan["collisions"][0]["live"] == ["sensor.system_monitor_memory_usage"]

    def test_no_safe_glob_ever_matches_a_live_entity(self):
        ghosts = [meta_row(e) for e in
                  ("switch.grafana_container", "sensor.grafana_memory_usage",
                   "sensor.grafana_health", "media_player.philips_tv_x")]
        live = {"sensor.system_monitor_memory_usage", "sensor.karmik_review_status",
                "sensor.backup_backup_manager_state"}
        plan = ghost_report.build_purge_plan(
            ghosts, live,
            globs=["*_container", "sensor.*_memory_usage", "sensor.*_health",
                   "media_player.philips_tv_*", "*_status", "*_state"])
        import fnmatch
        for glob in plan["safe_globs"]:
            assert not [e for e in live if fnmatch.fnmatch(e, glob)], glob

    def test_every_ghost_is_accounted_for_exactly_once(self):
        ghosts = [meta_row(e) for e in
                  ("switch.a_container", "sensor.b_memory_usage", "light.orphan")]
        plan = ghost_report.build_purge_plan(
            ghosts, live={"sensor.system_monitor_memory_usage"},
            globs=["*_container", "sensor.*_memory_usage"])
        assert set(plan["covered"]) | set(plan["explicit"]) == {g["entity_id"] for g in ghosts}
        assert set(plan["covered"]) & set(plan["explicit"]) == set()

    def test_generated_globs_are_also_safe_against_live_entities(self):
        """End to end: whatever the detector proposes still cannot hit a live one."""
        ghosts = containers("_memory_usage") + containers("_container")
        live = {"sensor.system_monitor_memory_usage", "light.kitchen"}
        families = ghost_report.affix_families(ghosts)
        groups = ghost_report.group_ghosts(ghosts, live, families)
        plan = ghost_report.build_purge_plan(
            ghosts, live, ghost_report.candidate_globs(families, groups))
        import fnmatch
        for glob in plan["safe_globs"]:
            assert not [e for e in live if fnmatch.fnmatch(e, glob)], glob
        assert "sensor.*_memory_usage" not in plan["safe_globs"]


class TestAffixFamilies:
    def test_repeated_suffix_across_many_names_is_a_family(self):
        families = ghost_report.affix_families(containers("_container"))
        assert [ghost_report.family_glob(k) for k in families] == ["sensor.*_container"]

    def test_suffix_seen_once_is_not_a_family(self):
        ghosts = [meta_row("switch.tv_screen_state")]
        assert ghost_report.affix_families(ghosts) == {}

    def test_most_specific_suffix_wins_over_the_one_it_contains(self):
        """_cpu_usage_total and _total both qualify; only the long form is kept."""
        families = ghost_report.affix_families(containers("_cpu_usage_total"))
        assert [k[1] for k in families] == ["_cpu_usage_total"]

    def test_shared_prefix_is_a_family_too(self):
        ghosts = [meta_row(f"sensor.local_{n}")
                  for n in ("llms", "openai", "whisper", "piper", "wakeword")]
        families = ghost_report.affix_families(ghosts)
        assert [ghost_report.family_glob(k) for k in families] == ["sensor.local_*"]

    def test_families_never_count_an_entity_twice(self):
        ghosts = containers("_cpu_usage_total") + containers("_memory_usage")
        families = ghost_report.affix_families(ghosts)
        assigned = [i for entry in families.values() for i in entry["ids"]]
        assert len(assigned) == len(set(assigned))


class TestSharedTokens:
    def test_fragment_common_to_ghosts_and_absent_from_live_forms_a_group(self):
        ghosts = [meta_row(f"media_player.65abc1234_{i}") for i in range(5)]
        groups = ghost_report.shared_tokens(ghosts, live={"light.kitchen"})
        assert [token for token, _ in groups] == ["65abc1234"]

    def test_fragment_still_common_among_live_entities_is_ignored(self):
        ghosts = [meta_row(f"sensor.bedroom_{n}") for n in
                  ("humidity", "battery", "linkquality", "voltage")]
        live = {f"sensor.bedroom_live_{i}" for i in range(40)}
        assert ghost_report.shared_tokens(ghosts, live) == []

    def test_suffix_family_words_do_not_become_groups(self):
        ghosts = containers("_cpu_usage_total")
        families = ghost_report.affix_families(ghosts)
        groups = ghost_report.shared_tokens(ghosts, live=set(),
                                            exclude=ghost_report.family_tokens(families))
        assert groups == []


class TestDeathCohorts:
    def test_entities_that_stopped_together_form_one_cohort(self):
        rows = [meta_row(f"sensor.a{i}", last_ts=T0 + i) for i in range(5)]
        cohorts, undated = ghost_report.death_cohorts(rows)
        assert len(cohorts) == 1 and len(cohorts[0]) == 5
        assert undated == []

    def test_a_long_silence_splits_two_removals(self):
        rows = ([meta_row(f"sensor.a{i}", last_ts=T0 + i) for i in range(3)]
                + [meta_row(f"sensor.b{i}", last_ts=T0 + 86400 + i) for i in range(3)])
        cohorts, _ = ghost_report.death_cohorts(rows)
        assert [len(c) for c in cohorts] == [3, 3]
        assert cohorts[0][0]["entity_id"].startswith("sensor.b"), "newest cohort first"

    def test_entities_with_no_rows_are_kept_apart(self):
        rows = [meta_row("sensor.a", last_ts=0), meta_row("sensor.b", last_ts=T0)]
        cohorts, undated = ghost_report.death_cohorts(rows)
        assert [r["entity_id"] for r in undated] == ["sensor.a"]
        assert [r["entity_id"] for c in cohorts for r in c] == ["sensor.b"]


class TestGrouping:
    def test_shared_token_wins_over_a_generic_suffix_family(self):
        """switch.<tv>_screen_state belongs with the TV, not with the _state family."""
        ghosts = (containers("_state")
                  + [meta_row("switch.65abc1234_12_screen_state"),
                     meta_row("media_player.65abc1234_12_1"),
                     meta_row("media_player.65abc1234_12_2"),
                     meta_row("sensor.65abc1234_12_volume")])
        groups = ghost_report.group_ghosts(ghosts, live=set())
        owner = next(g for g in groups
                     if any(m["entity_id"] == "switch.65abc1234_12_screen_state"
                            for m in g["members"]))
        assert owner["kind"] == "token" and owner["key"] == "65abc1234"

    def test_leftovers_are_grouped_by_when_they_went_silent(self):
        """Names with nothing in common still cluster by the moment of deletion."""
        names = ["sensor.attic_temperature", "sensor.garage_door",
                 "switch.pump_relay", "light.shed"]
        ghosts = [meta_row(e, last_ts=T0 + i) for i, e in enumerate(names)]
        groups = ghost_report.group_ghosts(ghosts, live=set())
        assert [g["kind"] for g in groups] == ["cohort"]

    def test_a_lone_ghost_lands_in_the_catch_all(self):
        groups = ghost_report.group_ghosts([meta_row("sensor.something_odd")],
                                           live=set())
        assert groups[-1]["kind"] == "other"

    def test_grouping_partitions_every_ghost(self):
        ghosts = (containers("_state")
                  + [meta_row("sensor.mobile_ssid", last_ts=T0 - 900000),
                     meta_row("sensor.mobile_battery", last_ts=T0 - 900000),
                     meta_row("sensor.mobile_steps", last_ts=T0 - 900000),
                     meta_row("sensor.mobile_bssid", last_ts=T0 - 900000),
                     meta_row("sensor.something_odd", last_ts=0)])
        groups = ghost_report.group_ghosts(ghosts, live=set())
        seen = [m["entity_id"] for g in groups for m in g["members"]]
        assert sorted(seen) == sorted(g["entity_id"] for g in ghosts)
        assert len(seen) == len(set(seen)), "no ghost may appear in two groups"


class TestPurgeYaml:
    def test_titles_match_the_block_they_describe(self):
        plan = {"safe_globs": ["*_container"], "covered": ["switch.a_container"],
                "explicit": ["light.orphan"], "collisions": []}
        blocks = ghost_report.purge_yaml(plan)
        assert [t for t, _ in blocks] == [
            "Safe globs · 1 entities", "Explicit list · 1 entities"]
        assert "entity_globs" in blocks[0][1]
        assert "entity_id" in blocks[1][1]

    def test_explicit_only_plan_is_not_mislabelled_as_globs(self):
        """Regression: with no usable globs the sole block is the explicit list."""
        plan = {"safe_globs": [], "covered": [],
                "explicit": ["light.orphan"], "collisions": []}
        blocks = ghost_report.purge_yaml(plan)
        assert len(blocks) == 1
        assert blocks[0][0].startswith("Explicit list")

    def test_empty_plan_yields_no_blocks(self):
        plan = {"safe_globs": [], "covered": [], "explicit": [], "collisions": []}
        assert ghost_report.purge_yaml(plan) == []

    def test_polish_titles_come_from_the_same_plan(self):
        plan = {"safe_globs": [], "covered": [],
                "explicit": ["light.orphan"], "collisions": []}
        assert ghost_report.purge_yaml(plan, "pl")[0][0].startswith("Lista jawna")


class TestDatabaseAccess:
    def test_sqlite_web_rejects_non_select_statements(self):
        for sql in ("DELETE FROM states", "DROP TABLE states", "UPDATE states SET x=1"):
            with pytest.raises(ValueError, match="Refusing non-SELECT"):
                ghost_report.sqlite_query("http://db.local:8088", sql)

    def test_direct_file_access_rejects_non_select_statements(self, tmp_path):
        with pytest.raises(ValueError, match="Refusing non-SELECT"):
            ghost_report.sqlite_file_query(tmp_path / "x.db", "DELETE FROM states")

    def test_allows_select_and_cte(self, monkeypatch):
        resp = MagicMock()
        resp.json.return_value = [{"entity_id": "light.kitchen"}]
        resp.raise_for_status.return_value = None
        monkeypatch.setattr(ghost_report.requests, "post", lambda *a, **k: resp)
        assert ghost_report.sqlite_query("http://db.local:8088", "SELECT 1")
        assert ghost_report.sqlite_query("http://db.local:8088", "WITH x AS (SELECT 1) SELECT * FROM x")

    def test_reads_a_real_sqlite_file_read_only(self, tmp_path):
        import sqlite3
        db = tmp_path / "recorder.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE states_meta (metadata_id INT, entity_id TEXT)")
        conn.execute("INSERT INTO states_meta VALUES (1, 'light.kitchen')")
        conn.commit()
        conn.close()
        rows = ghost_report.sqlite_file_query(db, "SELECT entity_id FROM states_meta")
        assert rows == [{"entity_id": "light.kitchen"}]

    def test_unsupported_backends_are_refused_not_half_handled(self):
        with pytest.raises(SystemExit, match="SQLite recorder files only"):
            ghost_report.sqlite_file_query(
                "mysql://ha:pw@192.0.2.5/homeassistant", "SELECT 1")

    def test_missing_file_fails_with_the_path_in_the_message(self, tmp_path):
        with pytest.raises(SystemExit, match="not found"):
            ghost_report.sqlite_file_query(tmp_path / "nope.db", "SELECT 1")


class TestRender:
    def test_renders_all_ghosts_and_closes_every_details(self):
        snap = make_snapshot(
            live=["light.kitchen", "sensor.system_monitor_memory_usage"],
            meta_ids=["light.kitchen", "sensor.system_monitor_memory_usage",
                      "sensor.grafana_state", "sensor.mobile_ssid",
                      "media_player.undefined_undefined_65abc1234_12_9"],
        )
        page = ghost_report.render(ghost_report.analyse(snap))
        for entity_id in ("sensor.grafana_state", "sensor.mobile_ssid",
                          "media_player.undefined_undefined_65abc1234_12_9"):
            assert entity_id in page
        assert page.count("<details") == page.count("</details>")
        assert "<title>" in page

    def test_theme_tokens_are_all_defined_in_base_root(self):
        """Every token overridden for dark must exist in :root, or the
        un-stamped 'system' theme renders with missing colors."""
        import re
        base = ghost_report.CSS.split("@media")[0]
        base_tokens = set(re.findall(r"(--[a-z0-9-]+)\s*:", base))
        for block in re.findall(r"\{([^{}]*--[^{}]*)\}", ghost_report.CSS):
            assert set(re.findall(r"(--[a-z0-9-]+)\s*:", block)) <= base_tokens

    def test_handles_a_database_with_no_ghosts(self):
        snap = make_snapshot(live=["light.kitchen"], meta_ids=["light.kitchen"])
        page = ghost_report.render(ghost_report.analyse(snap))
        assert "Nothing to clean up" in page

    def test_sections_with_no_data_are_omitted_entirely(self):
        """No '0 entities' headings above empty tables."""
        snap = make_snapshot(live=["light.kitchen"], meta_ids=["light.kitchen"])
        page = ghost_report.render(ghost_report.analyse(snap))
        assert "Recurring name patterns" not in page
        assert "Ghosts by origin" not in page

    def test_polish_renders_the_same_report(self):
        snap = make_snapshot(live=["light.kitchen"],
                             meta_ids=["light.kitchen", "sensor.grafana_state"])
        page = ghost_report.render(ghost_report.analyse(snap), "pl")
        assert "Duchy recordera" in page
        assert "sensor.grafana_state" in page

    def test_hide_source_keeps_the_database_address_off_the_page(self):
        """A report shown to someone else should not name the host it read."""
        snap = make_snapshot(live=["light.kitchen"],
                             meta_ids=["light.kitchen", "sensor.grafana_state"])
        snap["source"] = "http://192.0.2.5:8088"
        report = ghost_report.analyse(snap)
        assert "192.0.2.5" in ghost_report.render(report)
        assert "192.0.2.5" not in ghost_report.render(report, hide_source=True)
        assert "192.0.2.5" not in ghost_report.render(report, "pl", hide_source=True)

    def test_entity_names_are_escaped_into_group_headings(self):
        ghosts = [meta_row(f"sensor.a<b>_{i}") for i in range(5)]
        groups = ghost_report.group_ghosts(ghosts, live=set())
        assert "<b>" not in ghost_report.group_label(groups[0], "en")


class TestFmt:
    @pytest.mark.parametrize("value,expected",
                             [(0, "0"), (999, "999"),
                              (1234, "1 234"), (169645, "169 645")])
    def test_polish_groups_thousands_with_spaces(self, value, expected):
        """Separator is U+00A0 so numbers never wrap mid-value in the report."""
        assert ghost_report.fmt(value, "pl") == expected

    def test_english_uses_commas(self):
        assert ghost_report.fmt(169645) == "169,645"

    def test_decimal_separator_follows_the_language(self):
        assert ghost_report.pct(1.24) == "1.2%"
        assert ghost_report.pct(1.24, "pl") == "1,2 %"


@pytest.mark.skipif(not SNAPSHOT_PATH.exists(),
                    reason="reports/snapshot.json is gitignored local data")
class TestRealSnapshot:
    """Replay of a real installation: the detector has to hold up on messy data."""

    @pytest.fixture(scope="class")
    @classmethod
    def report(cls):
        import json
        return ghost_report.analyse(json.loads(SNAPSHOT_PATH.read_text()))

    def test_every_ghost_appears_in_exactly_one_group(self, report):
        seen = [m["entity_id"] for g in report["groups"] for m in g["members"]]
        assert len(seen) == len(set(seen)) == len(report["ghosts"])

    def test_purge_plan_partitions_the_ghosts(self, report):
        plan = report["plan"]
        assert (len(plan["covered"]) + len(plan["explicit"])
                == len(report["ghosts"]))

    def test_no_emitted_glob_touches_a_live_entity(self, report):
        import fnmatch
        live = set(report["snapshot"]["live"])
        for glob in report["plan"]["safe_globs"]:
            assert not [e for e in live if fnmatch.fnmatch(e, glob)], glob

    def test_detection_still_finds_structure_without_any_hardcoded_names(self, report):
        """The point of the rewrite: real ghosts must not all fall to 'other'."""
        assert report["families"], "no naming patterns detected"
        kinds = {g["kind"] for g in report["groups"]}
        assert {"token", "cohort"} & kinds
        other = sum(len(g["members"]) for g in report["groups"] if g["kind"] == "other")
        assert other < len(report["ghosts"]) / 2
