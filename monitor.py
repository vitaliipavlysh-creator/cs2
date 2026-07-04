"""
CS2 update watcher.

Polls two independent public signals for a new Counter-Strike 2 update:
  1. api.steamcmd.net mirror of Steam's app-info for app 730 (buildid/timeupdated
     on the "public" branch) - flips within seconds of Valve pushing a build.
  2. The official Counter-Strike blog RSS feed - usually gets a patch-notes post
     around the same time, and lets us flag posts mentioning "Armory Pass".

On either signal changing, fires a Pushover "emergency" notification (siren
sound, repeats until acknowledged, bypasses silent mode) and/or triggers an
outbound phone call via Twilio, depending on what's enabled in config.json.

State (last seen buildid / RSS guids) is persisted to state.json so a
restart doesn't cause a false trigger or miss a change that happened while
the script was down.
"""

import copy
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_EXAMPLE_PATH = BASE_DIR / "config.example.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "monitor.log"

STEAMCMD_INFO_URL = "https://api.steamcmd.net/v1/info/730"
CS_BLOG_FEED_URL = "https://blog.counter-strike.net/index.php/feed/"
KEYWORDS = ["armory pass", "armory", "case", "collection"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cs2-monitor")


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "cs2-update-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Failed to parse %s, ignoring", path)
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_config():
    # Prefer a real local config.json (desktop use); fall back to the
    # secret-free config.example.json (e.g. GitHub Actions, where secrets
    # come from environment variables instead of a committed file).
    if CONFIG_PATH.exists():
        config = load_json(CONFIG_PATH, {})
    elif CONFIG_EXAMPLE_PATH.exists():
        log.warning("No config.json found, falling back to config.example.json + env vars")
        config = load_json(CONFIG_EXAMPLE_PATH, {})
    else:
        log.error("Missing config.json - copy config.example.json to config.json and fill it in.")
        sys.exit(1)

    if os.environ.get("PUSHOVER_USER_KEY"):
        config.setdefault("pushover", {})["user_key"] = os.environ["PUSHOVER_USER_KEY"]
    if os.environ.get("PUSHOVER_API_TOKEN"):
        config.setdefault("pushover", {})["api_token"] = os.environ["PUSHOVER_API_TOKEN"]

    return config


def check_build(config, state):
    """Returns (changed: bool, info: dict|None)"""
    raw = http_get(STEAMCMD_INFO_URL)
    data = json.loads(raw)
    branch = data["data"]["730"]["depots"]["branches"]["public"]
    buildid = branch["buildid"]
    timeupdated = branch.get("timeupdated")

    last_buildid = state.get("last_buildid")
    if last_buildid is None:
        # First run: record baseline, don't alert.
        state["last_buildid"] = buildid
        state["last_timeupdated"] = timeupdated
        log.info("Baseline build recorded: %s", buildid)
        return False, None

    if buildid != last_buildid:
        info = {"old_buildid": last_buildid, "new_buildid": buildid, "timeupdated": timeupdated}
        state["last_buildid"] = buildid
        state["last_timeupdated"] = timeupdated
        return True, info

    return False, None


def check_blog(config, state):
    """Returns list of new entries: [{title, link, matched_keyword}]"""
    raw = http_get(CS_BLOG_FEED_URL)
    root = ET.fromstring(raw)
    seen_guids = set(state.get("seen_guids", []))
    new_entries = []

    items = root.findall("./channel/item")
    for item in items:
        guid_el = item.find("guid")
        link_el = item.find("link")
        title_el = item.find("title")
        guid = (guid_el.text if guid_el is not None else None) or (link_el.text if link_el is not None else None)
        if not guid or guid in seen_guids:
            continue
        seen_guids.add(guid)
        title = title_el.text if title_el is not None else "(no title)"
        link = link_el.text if link_el is not None else ""
        lowered = title.lower()
        matched = next((kw for kw in KEYWORDS if kw in lowered), None)
        new_entries.append({"title": title, "link": link, "matched_keyword": matched})

    # First run: don't flood with every historical post, just record them.
    if "seen_guids" not in state:
        state["seen_guids"] = list(seen_guids)
        return []

    state["seen_guids"] = list(seen_guids)
    return new_entries


def send_pushover(config, title, message, emergency=True):
    cfg = config.get("pushover", {})
    if not cfg.get("enabled"):
        return
    data = {
        "token": cfg["api_token"],
        "user": cfg["user_key"],
        "title": title,
        "message": message,
    }
    # Only override the sound if explicitly configured - otherwise defer to
    # whatever the user picked as their device's own high/emergency-priority
    # default sound (and its Critical Alerts / volume settings) in the app.
    if cfg.get("sound"):
        data["sound"] = cfg["sound"]

    priority_mode = cfg.get("priority_mode", "emergency" if emergency else "normal")
    if priority_mode == "emergency":
        data["priority"] = 2
        data["retry"] = 30
        data["expire"] = 3600
    elif priority_mode == "high":
        data["priority"] = 1
    else:
        data["priority"] = 0

    body = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in data.items())
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info("Pushover response: %s", resp.status)
    except Exception:
        log.exception("Failed to send Pushover notification")


def send_pushover_repeating(config, title, message):
    """Normal-priority pushes don't auto-retry like emergency ones do, so we
    fake it: fire the same notification repeatedly on a background thread.
    There's no ack detection for normal priority, so it just runs for a fixed
    window - stop it early by acknowledging/killing the process if you wake up."""
    cfg = config.get("pushover", {})
    if not cfg.get("enabled"):
        return
    interval = cfg.get("repeat_interval_seconds", 15)
    duration = cfg.get("repeat_duration_seconds", 600)
    count = max(1, duration // interval)

    def _worker():
        for i in range(count):
            send_pushover(config, title, message)
            if i < count - 1:
                time.sleep(interval)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def send_twilio_call(config, message):
    cfg = config.get("twilio", {})
    if not cfg.get("enabled"):
        return
    try:
        from twilio.rest import Client
    except ImportError:
        log.error("twilio package not installed (pip install twilio) - skipping call")
        return
    try:
        client = Client(cfg["account_sid"], cfg["auth_token"])
        twiml = f'<Response><Say loop="10">{message}</Say></Response>'
        client.calls.create(to=cfg["to_number"], from_=cfg["from_number"], twiml=twiml)
        log.info("Twilio call triggered")
    except Exception:
        log.exception("Failed to trigger Twilio call")


def alert(config, title, message):
    """Returns the repeat-worker thread if one was started, else None - lets
    short-lived callers (e.g. GitHub Actions `--once` runs) wait for the
    repeat sequence to actually finish before the process exits."""
    log.info("ALERT: %s | %s", title, message)
    thread = None
    cfg = config.get("pushover", {})
    if cfg.get("priority_mode") == "normal" and cfg.get("repeat_if_normal", True):
        thread = send_pushover_repeating(config, title, message)
    else:
        send_pushover(config, title, message, emergency=True)
    send_twilio_call(config, message)
    return thread


def run_test(config):
    # Single one-off push, not the full repeat-for-10-minutes behavior real
    # alerts use - so repeated manual `--test` runs don't stack up spam.
    send_pushover(config, "CS2 monitor test", "This is a test alert. If you got this loudly, you're set up correctly.")
    log.info("Test alert sent.")


def run_test_repeat(config):
    # Quick verification of the repeat mechanism (3 pushes, 10s apart)
    # without touching the real repeat_interval/duration config values.
    test_config = copy.deepcopy(config)
    test_config["pushover"]["repeat_interval_seconds"] = 10
    test_config["pushover"]["repeat_duration_seconds"] = 25
    alert(test_config, "CS2 monitor repeat test", "Repeat test: expect 3 pushes, 10s apart.")
    log.info("Repeat test started, waiting for it to finish...")
    time.sleep(30)
    log.info("Repeat test done.")


def run_once(config, state):
    """Single check-and-exit cycle, for ephemeral runners (GitHub Actions)
    that don't keep a process alive between scheduled invocations."""
    threads = []

    changed, build_info = check_build(config, state)
    if changed:
        t = alert(
            config,
            "CS2 UPDATE IS LIVE",
            f"Build changed {build_info['old_buildid']} -> {build_info['new_buildid']}. Get in there now.",
        )
        if t:
            threads.append(t)

    new_posts = check_blog(config, state)
    for entry in new_posts:
        if entry["matched_keyword"]:
            t = alert(
                config,
                "CS2 blog: possible Armory Pass post",
                f"{entry['title']}\n{entry['link']}",
            )
            if t:
                threads.append(t)
        else:
            log.info("New blog post (no keyword match): %s", entry["title"])

    save_json(STATE_PATH, state)

    # Block until any repeat-notification threads finish, otherwise the
    # process would exit and kill them after just one push.
    for t in threads:
        t.join()

    log.info("Single check complete.")


def main():
    config = load_config()
    state = load_json(STATE_PATH, {})
    interval = config.get("poll_interval_seconds", 60)

    if "--test" in sys.argv:
        run_test(config)
        return

    if "--test-repeat" in sys.argv:
        run_test_repeat(config)
        return

    if "--once" in sys.argv:
        run_once(config, state)
        return

    log.info("Starting CS2 update monitor, polling every %ss", interval)
    consecutive_failures = 0
    heartbeat_every = config.get("heartbeat_seconds", 300)
    last_heartbeat = time.time()

    while True:
        try:
            now = time.time()
            if now - last_heartbeat >= heartbeat_every:
                log.info("Heartbeat: still polling, last known buildid=%s", state.get("last_buildid"))
                last_heartbeat = now

            changed, build_info = check_build(config, state)
            if changed:
                alert(
                    config,
                    "CS2 UPDATE IS LIVE",
                    f"Build changed {build_info['old_buildid']} -> {build_info['new_buildid']}. Get in there now.",
                )

            new_posts = check_blog(config, state)
            for entry in new_posts:
                if entry["matched_keyword"]:
                    alert(
                        config,
                        "CS2 blog: possible Armory Pass post",
                        f"{entry['title']}\n{entry['link']}",
                    )
                else:
                    log.info("New blog post (no keyword match): %s", entry["title"])

            save_json(STATE_PATH, state)
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            log.exception("Poll cycle failed (%d consecutive failures)", consecutive_failures)

        time.sleep(interval)


if __name__ == "__main__":
    main()
