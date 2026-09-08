# Fantasy Season Tracker

A no-login, no-server dashboard for your Sleeper leagues: bye weeks, injury
status, and simple start/bench suggestions for every team in both leagues
(not just yours), so anyone in the league can pick their team and check it.

Pulls from Sleeper's public API (no auth needed), runs on a schedule via
GitHub Actions, and serves a static page from GitHub Pages — no server to
maintain, no hosting cost.

## How it works

1. `scripts/refresh_data.py` hits the Sleeper API for both configured
   leagues, pulls every roster, cross-references each player against
   `data/bye_weeks_2026.json` (bye weeks) and Sleeper's own injury status
   field, and writes `docs/data.json`.
2. `.github/workflows/refresh.yml` runs that script on a schedule (twice a
   day by default — see the cron line, tweak as you like) and commits the
   updated `docs/data.json` back to the repo.
3. `docs/index.html` is a static page that reads `docs/data.json`. Pick a
   league, then a team name, and it shows your starting lineup with alerts
   plus suggested bench swaps.
4. It's a basic PWA (`manifest.json` + `sw.js`) so "Add to Home Screen" on
   your phone gives you an app icon. No push notifications by design — you
   open it and check it, nothing pings you.

## One-time setup

1. **Create a GitHub repo** and push this folder to it (see commands
   below). Public or private both work — Pages and Actions work on private
   repos on the free tier too.
2. **Enable GitHub Pages**: repo Settings → Pages → Source: "Deploy from a
   branch" → Branch: `main`, folder: `/docs`. Save. GitHub gives you a URL
   like `https://<you>.github.io/<repo>/` — that's the link to share with
   your league.
3. **Enable Actions**: they're on by default for a repo you push yourself;
   nothing extra to do. The workflow will run on its schedule, or you can
   trigger it immediately from the Actions tab → "Refresh fantasy data" →
   "Run workflow".
4. **First data refresh**: either wait for the schedule, trigger the
   workflow manually (previous step), or run it locally once so the page
   isn't empty while you wait:
   ```
   pip install -r requirements.txt
   python scripts/refresh_data.py
   git add docs/data.json && git commit -m "Initial data" && git push
   ```

```
git init
git add .
git commit -m "Season tracker: bye weeks, injuries, start/sit"
git branch -M main
git remote add origin https://github.com/<you>/<repo-name>.git
git push -u origin main
```

## Configuration

- **League IDs** live at the top of `scripts/refresh_data.py`
  (`LEAGUE_IDS`). Find a league's ID in its Sleeper URL:
  `sleeper.com/leagues/<LEAGUE_ID>/team`. Both of your leagues are already
  in there.
- **Bye weeks** are in `data/bye_weeks_2026.json`, sourced from the 2026
  schedule release. Swap in a new file (and update the path in the script)
  each new season.
- **Refresh frequency**: edit the `cron` line in
  `.github/workflows/refresh.yml`. It's in UTC — the default (12:00 and
  21:00 UTC) is roughly 10pm and 7am Melbourne time; adjust for daylight
  saving or run more often on game days if you want.

## Start/sit logic — what it is today, and how to sharpen it

Right now `best_bench_replacement()` in `scripts/refresh_data.py` ranks
bench options by Sleeper's own `search_rank` (a popularity ranking, not a
real projection) — it's only used to break ties among *healthy* bench
players at the right position once a starter is flagged BYE/OUT/
QUESTIONABLE. It's a reasonable placeholder, not real advice.

To plug in your FantasyPros rankings from the draft assistant: export the
weekly PPR rankings CSV (same as you already do for the draft), load it
into a `{player_name_or_id: rank}` dict near the top of the script, and use
that instead of `search_rank` inside `best_bench_replacement()`. Everything
else (bye/injury detection, the dashboard) doesn't need to change.

## Notes / limitations

- Sleeper's full player database (`/players/nfl`) is a large download
  (several MB) and Sleeper asks that it not be pulled more than a handful
  of times a day — the cron schedule above respects that.
- Team defenses show up in rosters as a team abbreviation (e.g. `"DET"`)
  rather than a player ID; the script handles this separately from skill
  position players.
- This was scaffolded without the ability to hit the live Sleeper API from
  the build environment, so the request/response shapes were verified
  against your two real league IDs via manual fetches, and the core
  matching/alert logic was exercised against fixture data mirroring those
  real shapes — but it hasn't run end-to-end against live data yet. Run it
  locally or trigger the Action once and skim `docs/data.json` before you
  send the link around, in case any field name has shifted.
