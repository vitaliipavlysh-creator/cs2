import json
import threading
import urllib.error

import pytest

import monitor

BUILD_JSON = json.dumps(
    {"data": {"730": {"depots": {"branches": {"public": {"buildid": "111", "timeupdated": "1000"}}}}}}
).encode()

BLOG_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>Some Post</title><link>https://blog.counter-strike.net/1</link><guid>https://blog.counter-strike.net/?p=1</guid></item>
<item><title>Armory Pass is here</title><link>https://blog.counter-strike.net/2</link><guid>https://blog.counter-strike.net/?p=2</guid></item>
</channel></rss>
"""


# --- build (Steam) checks ---


def test_parse_build_info():
    buildid, timeupdated = monitor.parse_build_info(BUILD_JSON)
    assert buildid == "111"
    assert timeupdated == "1000"


def test_check_build_first_run_baselines_without_alert(monkeypatch):
    monkeypatch.setattr(monitor, "http_get", lambda url, timeout=15: BUILD_JSON)
    state = {}
    changed, info = monitor.check_build({}, state)
    assert changed is False
    assert info is None
    assert state["last_buildid"] == "111"


def test_check_build_detects_change(monkeypatch):
    monkeypatch.setattr(monitor, "http_get", lambda url, timeout=15: BUILD_JSON)
    state = {"last_buildid": "999", "last_timeupdated": "1"}
    changed, info = monitor.check_build({}, state)
    assert changed is True
    assert info == {"old_buildid": "999", "new_buildid": "111", "timeupdated": "1000"}
    assert state["last_buildid"] == "111"


def test_check_build_no_change(monkeypatch):
    monkeypatch.setattr(monitor, "http_get", lambda url, timeout=15: BUILD_JSON)
    state = {"last_buildid": "111", "last_timeupdated": "1000"}
    changed, info = monitor.check_build({}, state)
    assert changed is False
    assert info is None


# --- blog checks ---


def test_parse_blog_items():
    items = monitor.parse_blog_items(BLOG_XML)
    assert len(items) == 2
    assert items[0] == {
        "guid": "https://blog.counter-strike.net/?p=1",
        "title": "Some Post",
        "link": "https://blog.counter-strike.net/1",
    }


@pytest.mark.parametrize(
    "title,expected",
    [
        ("The Armory Pass has arrived", "armory pass"),
        ("New Collection revealed", "collection"),
        ("Random hotfix", None),
    ],
)
def test_match_keyword(title, expected):
    assert monitor.match_keyword(title) == expected


def test_check_blog_first_run_baselines_without_returning_entries(monkeypatch):
    monkeypatch.setattr(monitor, "http_get", lambda url, timeout=15: BLOG_XML)
    state = {}
    entries = monitor.check_blog({}, state)
    assert entries == []
    assert set(state["seen_guids"]) == {
        "https://blog.counter-strike.net/?p=1",
        "https://blog.counter-strike.net/?p=2",
    }


def test_check_blog_detects_new_entries_and_keyword_match(monkeypatch):
    monkeypatch.setattr(monitor, "http_get", lambda url, timeout=15: BLOG_XML)
    state = {"seen_guids": ["https://blog.counter-strike.net/?p=1"]}
    entries = monitor.check_blog({}, state)
    assert len(entries) == 1
    assert entries[0]["title"] == "Armory Pass is here"
    assert entries[0]["matched_keyword"] == "armory pass"


# --- Pushover payload / sending ---


def test_build_pushover_payload_emergency():
    cfg = {"api_token": "t", "user_key": "u", "priority_mode": "emergency"}
    data = monitor.build_pushover_payload(cfg, "T", "M")
    assert data["priority"] == 2
    assert data["retry"] == 30
    assert data["expire"] == 3600


def test_build_pushover_payload_normal():
    cfg = {"api_token": "t", "user_key": "u", "priority_mode": "normal"}
    data = monitor.build_pushover_payload(cfg, "T", "M")
    assert data["priority"] == 0
    assert "retry" not in data


def test_build_pushover_payload_high():
    cfg = {"api_token": "t", "user_key": "u", "priority_mode": "high"}
    data = monitor.build_pushover_payload(cfg, "T", "M")
    assert data["priority"] == 1


def test_build_pushover_payload_sound_only_when_configured():
    cfg = {"api_token": "t", "user_key": "u", "priority_mode": "normal"}
    assert "sound" not in monitor.build_pushover_payload(cfg, "T", "M")
    cfg["sound"] = "siren"
    assert monitor.build_pushover_payload(cfg, "T", "M")["sound"] == "siren"


def test_build_pushover_payload_default_priority_from_emergency_flag():
    cfg = {"api_token": "t", "user_key": "u"}
    assert monitor.build_pushover_payload(cfg, "T", "M", emergency=False)["priority"] == 0
    assert monitor.build_pushover_payload(cfg, "T", "M", emergency=True)["priority"] == 2


def test_send_pushover_disabled_returns_true():
    config = {"pushover": {"enabled": False}}
    assert monitor.send_pushover(config, "T", "M") is True


def test_send_pushover_success(monkeypatch):
    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(monitor.urllib.request, "urlopen", lambda req, timeout=15: FakeResp())
    config = {"pushover": {"enabled": True, "api_token": "t", "user_key": "u", "priority_mode": "normal"}}
    assert monitor.send_pushover(config, "T", "M") is True


def test_send_pushover_http_error_returns_false(monkeypatch):
    def raise_error(req, timeout=15):
        raise urllib.error.HTTPError("url", 400, "Bad Request", {}, None)

    monkeypatch.setattr(monitor.urllib.request, "urlopen", raise_error)
    config = {"pushover": {"enabled": True, "api_token": "t", "user_key": "u", "priority_mode": "normal"}}
    assert monitor.send_pushover(config, "T", "M") is False


def test_send_pushover_repeating_collects_results(monkeypatch):
    monkeypatch.setattr(monitor, "send_pushover", lambda *a, **k: True)
    config = {"pushover": {"enabled": True, "repeat_interval_seconds": 1, "repeat_duration_seconds": 2}}
    thread = monitor.send_pushover_repeating(config, "T", "M")
    thread.join(timeout=5)
    assert thread.results == [True, True]


def test_alert_normal_mode_returns_thread(monkeypatch):
    monkeypatch.setattr(monitor, "send_pushover", lambda *a, **k: True)
    config = {
        "pushover": {
            "enabled": True,
            "priority_mode": "normal",
            "repeat_if_normal": True,
            "repeat_interval_seconds": 1,
            "repeat_duration_seconds": 1,
        }
    }
    result = monitor.alert(config, "T", "M")
    assert isinstance(result, threading.Thread)
    result.join(timeout=5)


def test_alert_emergency_mode_returns_bool(monkeypatch):
    monkeypatch.setattr(monitor, "send_pushover", lambda *a, **k: True)
    config = {"pushover": {"enabled": True, "priority_mode": "emergency"}}
    assert monitor.alert(config, "T", "M") is True


# --- config loading ---


def test_load_config_prefers_config_json(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"pushover": {"user_key": "real"}}))
    monkeypatch.setattr(monitor, "CONFIG_PATH", config_path)
    monkeypatch.setattr(monitor, "CONFIG_EXAMPLE_PATH", tmp_path / "config.example.json")
    config = monitor.load_config()
    assert config["pushover"]["user_key"] == "real"


def test_load_config_falls_back_to_example_with_env_override(tmp_path, monkeypatch):
    example_path = tmp_path / "config.example.json"
    example_path.write_text(json.dumps({"pushover": {"user_key": "placeholder", "api_token": "placeholder"}}))
    monkeypatch.setattr(monitor, "CONFIG_PATH", tmp_path / "config.json")  # doesn't exist
    monkeypatch.setattr(monitor, "CONFIG_EXAMPLE_PATH", example_path)
    monkeypatch.setenv("PUSHOVER_USER_KEY", "from_env")
    monkeypatch.setenv("PUSHOVER_API_TOKEN", "from_env_token")
    config = monitor.load_config()
    assert config["pushover"]["user_key"] == "from_env"
    assert config["pushover"]["api_token"] == "from_env_token"


# --- poll cycle / run_once orchestration ---


def test_run_poll_cycle_no_changes_returns_true(monkeypatch):
    monkeypatch.setattr(monitor, "check_build", lambda config, state: (False, None))
    monkeypatch.setattr(monitor, "check_blog", lambda config, state: [])
    assert monitor.run_poll_cycle({}, {}) is True


def test_run_poll_cycle_build_change_alert_failure_returns_false(monkeypatch):
    monkeypatch.setattr(
        monitor,
        "check_build",
        lambda config, state: (True, {"old_buildid": "1", "new_buildid": "2", "timeupdated": "t"}),
    )
    monkeypatch.setattr(monitor, "check_blog", lambda config, state: [])
    monkeypatch.setattr(monitor, "alert", lambda config, title, message: False)
    assert monitor.run_poll_cycle({}, {}) is False


def test_run_once_exits_nonzero_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(monitor, "run_poll_cycle", lambda config, state: False)
    with pytest.raises(SystemExit) as exc_info:
        monitor.run_once({}, {})
    assert exc_info.value.code == 1


def test_run_once_saves_state_with_timestamp(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(monitor, "STATE_PATH", state_path)
    monkeypatch.setattr(monitor, "run_poll_cycle", lambda config, state: True)
    state = {}
    monitor.run_once({}, state)
    assert "last_checked_at" in state
    saved = json.loads(state_path.read_text())
    assert saved["last_checked_at"] == state["last_checked_at"]
