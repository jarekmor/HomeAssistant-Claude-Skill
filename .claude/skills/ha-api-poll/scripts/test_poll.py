"""Unit tests for poll.py.

Covers argument/URL/param building and env/error handling with mocked HTTP —
no real Home Assistant instance is contacted.
"""
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent / "poll.py"
spec = importlib.util.spec_from_file_location("poll", SCRIPT_PATH)
poll = importlib.util.module_from_spec(spec)
sys.modules["poll"] = poll
spec.loader.exec_module(poll)


@pytest.fixture
def env_creds(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://ha.local:8123")
    monkeypatch.setenv("HA_TOKEN", "test-token")


@pytest.fixture
def mock_session(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(poll.requests, "Session", lambda: session)
    return session


def make_response(status_code=200, json_data=None, text_data=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = text_data
    resp.raise_for_status.return_value = None
    return resp


class TestLoadEnvFile:
    def test_loads_missing_vars_from_dotenv(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text('HA_URL="http://from-dotenv:8123"\nHA_TOKEN=abc123\n')
        monkeypatch.setattr(poll, "PROJECT_ROOT", tmp_path)
        monkeypatch.delenv("HA_URL", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)

        poll.load_env_file()

        assert os.environ["HA_URL"] == "http://from-dotenv:8123"
        assert os.environ["HA_TOKEN"] == "abc123"

    def test_does_not_override_existing_env(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("HA_TOKEN=from-dotenv\n")
        monkeypatch.setattr(poll, "PROJECT_ROOT", tmp_path)
        monkeypatch.setenv("HA_TOKEN", "already-set")

        poll.load_env_file()

        assert os.environ["HA_TOKEN"] == "already-set"

    def test_skips_comments_and_blank_lines(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\n\nHA_URL=http://x\nnot_a_line_without_equals\n")
        monkeypatch.setattr(poll, "PROJECT_ROOT", tmp_path)
        monkeypatch.delenv("HA_URL", raising=False)

        poll.load_env_file()

        assert os.environ["HA_URL"] == "http://x"

    def test_missing_dotenv_file_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(poll, "PROJECT_ROOT", tmp_path)
        poll.load_env_file()  # should not raise


class TestGetSession:
    def test_exits_when_credentials_missing(self, monkeypatch):
        monkeypatch.delenv("HA_URL", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)
        monkeypatch.setattr(poll, "load_env_file", lambda: None)

        with pytest.raises(SystemExit, match="Missing HA_URL"):
            poll.get_session()

    def test_builds_session_with_auth_header(self, env_creds, monkeypatch):
        monkeypatch.setattr(poll, "load_env_file", lambda: None)

        session, base_url = poll.get_session()

        assert base_url == "http://ha.local:8123"
        assert session.headers["Authorization"] == "Bearer test-token"
        assert session.headers["Content-Type"] == "application/json"

    def test_strips_trailing_slash_from_base_url(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://ha.local:8123/")
        monkeypatch.setenv("HA_TOKEN", "test-token")
        monkeypatch.setattr(poll, "load_env_file", lambda: None)

        _, base_url = poll.get_session()

        assert base_url == "http://ha.local:8123"


class TestRequest:
    def test_returns_parsed_json_by_default(self, mock_session):
        mock_session.get.return_value = make_response(json_data={"ok": True})

        result = poll.request(mock_session, "http://ha.local", "/api/config")

        assert result == {"ok": True}
        mock_session.get.assert_called_once_with(
            "http://ha.local/api/config", params=None, timeout=15
        )

    def test_passes_through_params(self, mock_session):
        mock_session.get.return_value = make_response(json_data=[])

        poll.request(mock_session, "http://ha.local", "/api/history/period", params={"a": "b"})

        _, kwargs = mock_session.get.call_args
        assert kwargs["params"] == {"a": "b"}

    def test_as_json_false_returns_text(self, mock_session):
        mock_session.get.return_value = make_response(text_data="plain text log")

        result = poll.request(mock_session, "http://ha.local", "/api/error_log", as_json=False)

        assert result == "plain text log"

    def test_404_exits_with_message(self, mock_session):
        mock_session.get.return_value = make_response(status_code=404)

        with pytest.raises(SystemExit, match="404 Not Found"):
            poll.request(mock_session, "http://ha.local", "/api/states/light.nope")

    def test_401_exits_with_message(self, mock_session):
        mock_session.get.return_value = make_response(status_code=401)

        with pytest.raises(SystemExit, match="401 Unauthorized"):
            poll.request(mock_session, "http://ha.local", "/api/states")

    def test_other_error_status_raises_via_raise_for_status(self, mock_session):
        import requests

        resp = make_response(status_code=500)
        resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        mock_session.get.return_value = resp

        with pytest.raises(requests.HTTPError):
            poll.request(mock_session, "http://ha.local", "/api/config")


def test_output_prints_plain_string(capsys):
    poll.output("hello")
    assert capsys.readouterr().out == "hello\n"


def test_output_prints_json_for_dict(capsys):
    poll.output({"a": 1})
    captured = capsys.readouterr().out
    assert '"a": 1' in captured


@pytest.mark.parametrize(
    "argv,expected_path,expected_entity",
    [
        (["states"], "/api/states", None),
        (["states", "light.office"], "/api/states/light.office", "light.office"),
    ],
)
def test_states_dispatch(mock_session, env_creds, monkeypatch, capsys, argv, expected_path, expected_entity):
    monkeypatch.setattr(poll, "load_env_file", lambda: None)
    monkeypatch.setattr(sys, "argv", ["poll.py", *argv])
    mock_session.get.return_value = make_response(json_data={"state": "on"})

    poll.main()

    called_url = mock_session.get.call_args[0][0]
    assert called_url == f"http://ha.local:8123{expected_path}"


def test_states_encodes_entity_id(mock_session, env_creds, monkeypatch):
    monkeypatch.setattr(poll, "load_env_file", lambda: None)
    monkeypatch.setattr(sys, "argv", ["poll.py", "states", "light.office/weird id"])
    mock_session.get.return_value = make_response(json_data={})

    poll.main()

    called_url = mock_session.get.call_args[0][0]
    assert "light.office" in called_url
    assert " " not in called_url


def test_history_builds_params_and_timestamp_path(mock_session, env_creds, monkeypatch):
    monkeypatch.setattr(poll, "load_env_file", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "poll.py", "history", "sensor.temperature",
            "--start", "2026-07-31T00:00:00+00:00",
            "--end", "2026-08-01T00:00:00+00:00",
            "--minimal-response", "--no-attributes", "--significant-changes-only",
        ],
    )
    mock_session.get.return_value = make_response(json_data=[])

    poll.main()

    called_url, kwargs = mock_session.get.call_args[0][0], mock_session.get.call_args[1]
    assert called_url == "http://ha.local:8123/api/history/period/2026-07-31T00:00:00+00:00"
    assert kwargs["params"] == {
        "filter_entity_id": "sensor.temperature",
        "end_time": "2026-08-01T00:00:00+00:00",
        "minimal_response": "",
        "no_attributes": "",
        "significant_changes_only": "",
    }


def test_history_omits_start_segment_when_not_given(mock_session, env_creds, monkeypatch):
    monkeypatch.setattr(poll, "load_env_file", lambda: None)
    monkeypatch.setattr(sys, "argv", ["poll.py", "history", "sensor.temperature"])
    mock_session.get.return_value = make_response(json_data=[])

    poll.main()

    called_url = mock_session.get.call_args[0][0]
    assert called_url == "http://ha.local:8123/api/history/period"


def test_logbook_with_entity_filter(mock_session, env_creds, monkeypatch):
    monkeypatch.setattr(poll, "load_env_file", lambda: None)
    monkeypatch.setattr(sys, "argv", ["poll.py", "logbook", "--entity", "light.office"])
    mock_session.get.return_value = make_response(json_data=[])

    poll.main()

    called_url, kwargs = mock_session.get.call_args[0][0], mock_session.get.call_args[1]
    assert called_url == "http://ha.local:8123/api/logbook"
    assert kwargs["params"] == {"entity": "light.office"}


def test_calendar_requires_start_and_end(mock_session, env_creds, monkeypatch):
    monkeypatch.setattr(poll, "load_env_file", lambda: None)
    monkeypatch.setattr(
        sys, "argv",
        ["poll.py", "calendar", "calendar.home", "--start", "2026-08-01T00:00:00", "--end", "2026-08-08T00:00:00"],
    )
    mock_session.get.return_value = make_response(json_data=[])

    poll.main()

    called_url, kwargs = mock_session.get.call_args[0][0], mock_session.get.call_args[1]
    assert called_url == "http://ha.local:8123/api/calendars/calendar.home"
    assert kwargs["params"] == {"start": "2026-08-01T00:00:00", "end": "2026-08-08T00:00:00"}


def test_calendar_missing_required_args_exits(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["poll.py", "calendar", "calendar.home"])
    with pytest.raises(SystemExit):
        poll.main()


def test_get_escape_hatch_builds_params_from_repeated_flags(mock_session, env_creds, monkeypatch):
    monkeypatch.setattr(poll, "load_env_file", lambda: None)
    monkeypatch.setattr(
        sys, "argv",
        ["poll.py", "get", "/api/states/light.office", "--param", "a=1", "--param", "b=2"],
    )
    mock_session.get.return_value = make_response(json_data={})

    poll.main()

    called_url, kwargs = mock_session.get.call_args[0][0], mock_session.get.call_args[1]
    assert called_url == "http://ha.local:8123/api/states/light.office"
    assert kwargs["params"] == {"a": "1", "b": "2"}


def test_get_adds_leading_slash_if_missing(mock_session, env_creds, monkeypatch):
    monkeypatch.setattr(poll, "load_env_file", lambda: None)
    monkeypatch.setattr(sys, "argv", ["poll.py", "get", "api/config"])
    mock_session.get.return_value = make_response(json_data={})

    poll.main()

    called_url = mock_session.get.call_args[0][0]
    assert called_url == "http://ha.local:8123/api/config"


def test_errors_uses_plaintext_response(mock_session, env_creds, monkeypatch, capsys):
    monkeypatch.setattr(poll, "load_env_file", lambda: None)
    monkeypatch.setattr(sys, "argv", ["poll.py", "errors"])
    mock_session.get.return_value = make_response(text_data="ERROR: something broke")

    poll.main()

    assert "ERROR: something broke" in capsys.readouterr().out
