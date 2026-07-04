# CS2 Update Monitor

Polls two independent signals for a new CS2 update and fires a loud, hard-to-sleep-through
Pushover alert (siren sound, repeats until you acknowledge it, bypasses silent mode):

1. `api.steamcmd.net` mirror of Steam's app info for app 730 — the `buildid` on the
   `public` branch changes the moment Valve pushes a build, usually seconds before
   anything else notices.
2. The official Counter-Strike blog RSS feed — flags any new post whose title mentions
   "armory", "armory pass", "case", or "collection".

Either signal alone can false-trigger occasionally (e.g. a blog post about something
unrelated); having both means you get context in the alert message either way.

## 1. One-time setup

```
pip install -r requirements.txt   # only needed if you enable Twilio calls
```

Copy the config template and fill it in:

```
copy config.example.json config.json
```

### Set up Pushover (recommended, ~$5 one-time app purchase)

1. Create an account at pushover.net and install the Pushover app on your phone.
2. Your **User Key** is on the pushover.net dashboard.
3. Create an "Application" (any name, e.g. "CS2 Monitor") to get an **API Token**.
4. Put both into `config.json` under `pushover`.
5. In the Pushover app, make sure notification sounds/priority overrides are allowed
   past Do Not Disturb (Settings -> Emergency Priority) — this is what makes it
   actually wake you up instead of silently landing like a normal push.

### (Optional) Twilio phone call as a second layer

Only bother with this if you want an actual ringing call as backup. Requires a
Twilio account, a Twilio phone number, and costs a small per-call fee. Fill in the
`twilio` block in `config.json` and set `"enabled": true`.

## 2. Test it before relying on it

```
python monitor.py --test
```

You should get a loud Pushover alert within a few seconds. Don't skip this —
confirm it actually wakes you up (try it once with your phone in another room,
under a pillow, etc.) before trusting it overnight.

## 3. Make it run 24/7 on this PC

Since this only works while your PC is on, powered, and connected — disable sleep/
hibernate for the nights you're expecting the update (Windows Settings -> System ->
Power & sleep -> set "Sleep" to Never), and set it to auto-start:

**Easiest: Startup folder**
1. Press `Win+R`, type `shell:startup`, Enter.
2. Create a shortcut to `run_monitor.bat` in that folder.
3. It'll launch next time you log in, and `run_monitor.bat` auto-restarts
   `monitor.py` if it ever crashes (logged to `monitor_wrapper.log`).

**More robust: Task Scheduler** (survives without you being logged in, if you set
a password-less auto-login or "run whether user is logged on or not"):
1. Open Task Scheduler -> Create Task.
2. General tab: check "Run whether user is logged on or not".
3. Triggers tab: New -> "At log on" (or "At startup" if using auto-login).
4. Actions tab: New -> Program: `run_monitor.bat`, Start in: this folder.
5. Settings tab: check "If the task fails, restart every 1 minute", set restart
   attempts high (e.g. 999).

## 4. Run it for real

Just leave it running (`run_monitor.bat`, or let Task Scheduler start it) starting
a day or two before you expect the update. Check `monitor.log` occasionally to
confirm it's actually polling (it logs each cycle's baseline and any state changes).

## 5. Alternative: run it for free on GitHub Actions (no PC needed)

Instead of (or in addition to) running on your own PC, GitHub can run the check
on a schedule for free, on their servers, so your PC doesn't need to stay on.
Trade-off: it checks roughly every 10 minutes instead of every 60 seconds
(GitHub's scheduled workflows aren't meant for tighter intervals and can also
run a few minutes late during busy periods).

1. **Create a GitHub repo** (private recommended) and push this folder to it:
   ```
   git init
   git add .
   git commit -m "CS2 update monitor"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```
   `config.json` is gitignored on purpose — your real Pushover keys never get
   committed. GitHub Actions reads them from encrypted Secrets instead (next step).

2. **Add your Pushover credentials as repo secrets** (Settings -> Secrets and
   variables -> Actions -> "New repository secret"):
   - `PUSHOVER_USER_KEY` — your User Key
   - `PUSHOVER_API_TOKEN` — your Application's API Token

3. That's it — [.github/workflows/cs2-monitor.yml](.github/workflows/cs2-monitor.yml)
   is already set up to run `python monitor.py --once` every 10 minutes and commit
   the updated `state.json` back to the repo so state persists between runs.
   You can trigger it manually right away from the repo's Actions tab ("Run workflow")
   to confirm it works without waiting 10 minutes.

4. Check the Actions tab occasionally (or the commit history on `state.json`) to
   confirm it's still running — GitHub auto-disables scheduled workflows after
   60 days with zero commits to the repo, but since every run commits `state.json`,
   that resets the clock automatically as long as it keeps running successfully.

## Tests

The pure logic (parsing, payload building, poll-cycle orchestration) is covered by
unit tests, with network calls mocked out - no real Pushover/Steam/blog traffic:

```
pip install -r requirements-dev.txt
python -m pytest -v
```

[.github/workflows/tests.yml](.github/workflows/tests.yml) runs the suite automatically
on every push that touches `monitor.py` or the tests.

## Notes / limitations

- This can only alert you as fast as `poll_interval_seconds` (default 60s) plus
  however fast Valve's build propagates to the steamcmd mirror — in practice
  this is usually faster than watching Steam's own "Downloading update" prompt.
- If your home internet drops overnight, the monitor can't see anything until it's
  back — there's no separate connectivity watchdog here. Running on GitHub Actions
  (section 5) avoids this specific risk since it's not dependent on your home
  internet or PC at all, at the cost of a slower ~10 minute check interval.
- Being "first to spend stars" also depends on the in-game shop/inventory actually
  being live for you the moment the update lands, which can lag slightly behind
  the build/blog signals this watches for.
