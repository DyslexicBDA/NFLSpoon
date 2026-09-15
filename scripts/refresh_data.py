#!/usr/bin/env python3
"""
Pulls roster data for a set of Sleeper fantasy leagues, cross-references it
against injury status (from Sleeper) and bye weeks (from data/bye_weeks_*.json),
and writes one JSON file the static dashboard (docs/index.html) reads.

Runs unauthenticated against the public Sleeper API - no API key needed.
Intended to run on a schedule via GitHub Actions (see .github/workflows/refresh.yml),
but works fine run locally too: `python scripts/refresh_data.py`.

NOTE: this covers every roster in every configured league (not just one team),
so anyone in the league can look up their own team in the dashboard.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.sleeper.app/v1"

# Add / remove league IDs here. Find a league's ID in its Sleeper URL:
# https://sleeper.com/leagues/<LEAGUE_ID>/team
LEAGUE_IDS = [
    "1389689898046361600",
    "1389343132620963840",
]

# Injury statuses Sleeper uses. "Questionable" is a soft flag; the others are
# hard flags (the player is a real risk not to play).
HARD_OUT_STATUSES = {"Out", "Doubtful", "IR", "PUP", "Suspended", "NA"}
SOFT_FLAG_STATUSES = {"Questionable"}

BYE_WEEK_FILE = ROOT / "data" / "bye_weeks_2026.json"
OUTPUT_FILE = ROOT / "docs" / "data.json"


def get(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def load_bye_weeks():
    with open(BYE_WEEK_FILE) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_all_players():
    """The full Sleeper player DB (~5-10MB). Sleeper asks that this only be
    pulled a handful of times a day, not on every request."""
    return get(f"{API}/players/nfl")


def build_player_lookup(players_raw, bye_weeks):
    """Normalize the fields we actually use. Handles team defenses (Sleeper
    represents e.g. the Lions D/ST with the roster player_id "DET", not an
    entry in /players/nfl)."""
    lookup = {}
    for pid, p in players_raw.items():
        team = p.get("team")
        lookup[pid] = {
            "player_id": pid,
            "name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
            "position": p.get("position"),
            "team": team,
            "bye_week": bye_weeks.get(team) if team else None,
            "injury_status": p.get("injury_status"),
            "injury_body_part": p.get("injury_body_part"),
            "search_rank": p.get("search_rank") if p.get("search_rank") is not None else 999999,
        }
    return lookup


def defense_entry(team_abbr, bye_weeks):
    return {
        "player_id": team_abbr,
        "name": f"{team_abbr} D/ST",
        "position": "DEF",
        "team": team_abbr,
        "bye_week": bye_weeks.get(team_abbr),
        "injury_status": None,
        "injury_body_part": None,
        "search_rank": 999999,
    }


def resolve_player(pid, player_lookup, bye_weeks):
    if pid in player_lookup:
        return player_lookup[pid]
    # Not in the player DB -> almost certainly a team defense (e.g. "DET").
    return defense_entry(pid, bye_weeks)


def player_status(player, current_week):
    """Returns one of: OK, BYE, QUESTIONABLE, OUT (Out/Doubtful/IR/etc)."""
    if player["bye_week"] == current_week:
        return "BYE"
    status = player["injury_status"]
    if status in HARD_OUT_STATUSES:
        return "OUT"
    if status in SOFT_FLAG_STATUSES:
        return "QUESTIONABLE"
    return "OK"


def load_weekly_stats(season, upto_week):
    """Raw per-player stat categories (receptions, yards, TDs, etc.) for
    every player in the NFL, one week at a time. This is NOT league-specific
    - it's the same data regardless of which league you're in - so it's
    fetched once per run and shared across every configured league, then
    combined with each league's own scoring_settings to work out how many
    fantasy points a player *not on any roster* would have scored. Rostered
    players don't need this: their real weekly score already comes straight
    from the league's own matchups (see build_weekly_aggregates).
    """
    return {week: get(f"{API}/stats/nfl/regular/{season}/{week}") for week in range(1, upto_week + 1)}


def compute_points(stat_entry, scoring_settings):
    """Turns one player's raw weekly stats into fantasy points using this
    league's own scoring_settings - the same mechanism Sleeper itself uses
    (points = sum of each stat category's count times that league's point
    value for it). A stat category with no entry in scoring_settings simply
    contributes nothing.
    """
    if not stat_entry:
        return 0.0
    return sum(stat_entry.get(cat, 0) * weight for cat, weight in scoring_settings.items())


def load_league_weekly_matchups(league_id, upto_week):
    return {week: get(f"{API}/league/{league_id}/matchups/{week}") for week in range(1, upto_week + 1)}


def build_weekly_aggregates(matchups_by_week):
    """From a league's own week-by-week matchups, build:
      - player_weekly_points: {player_id: {week: points}} for every player
        who was on ANY roster in this league that week (Sleeper's own
        computed score for them, in this league's real scoring settings).
      - team_weekly_points: {roster_id: {week: points}} - that team's total
        for the week, preferring a commissioner's custom_points override if
        one was set.
    This only ever covers players who were rostered at some point - a
    player who has sat on waivers all season won't appear here at all,
    which is exactly the signal used to fall back to compute_points() for
    free agents.

    Sleeper still returns a fully zero-filled matchups entry for a week
    whose games haven't been played yet, rather than omitting it - if every
    single team scored exactly 0 that week, it hasn't happened yet (real
    football weeks always produce *some* points across a whole league), so
    that week is skipped entirely rather than counted as "a real game where
    everyone scored zero", which would otherwise drag PPG down for no
    reason before the games have even kicked off.
    """
    player_weekly_points = {}
    team_weekly_points = {}
    for week, entries in matchups_by_week.items():
        week_has_scores = any((e.get("custom_points") or e.get("points") or 0) for e in entries)
        if not week_has_scores:
            continue
        for entry in entries:
            roster_id = entry["roster_id"]
            custom = entry.get("custom_points")
            team_weekly_points.setdefault(roster_id, {})[week] = custom if custom is not None else entry.get("points", 0.0)
            for pid, pts in (entry.get("players_points") or {}).items():
                player_weekly_points.setdefault(pid, {})[week] = pts
    return player_weekly_points, team_weekly_points


def rostered_player_summary(pid, player_weekly_points, recent_n=3):
    """Season total, games-played count, season PPG and recent-N-week PPG
    for a player who has been on a roster in this league this season,
    sourced from that league's own matchup history. A week only counts as
    "played" if the player was actually on a roster that week and has a
    recorded score for it - so a player who joined the league mid-season
    (via trade or waivers) is correctly averaged over just the weeks they
    were actually rostered, not the full season.
    """
    weekly = player_weekly_points.get(pid, {})
    if not weekly:
        return {"weekly_points": {}, "season_points": 0.0, "games_played": 0, "ppg": None, "recent_ppg": None}
    weeks_sorted = sorted(weekly)
    season_points = sum(weekly.values())
    games_played = len(weeks_sorted)
    recent_weeks = weeks_sorted[-recent_n:]
    recent_ppg = sum(weekly[w] for w in recent_weeks) / len(recent_weeks)
    return {
        "weekly_points": {w: round(weekly[w], 2) for w in weeks_sorted},
        "season_points": round(season_points, 2),
        "games_played": games_played,
        "ppg": round(season_points / games_played, 2),
        "recent_ppg": round(recent_ppg, 2),
    }


def free_agent_summary(pid, weekly_stats, scoring_settings, recent_n=3):
    """Same shape as rostered_player_summary, but for a player nobody has
    rostered this season: computed from raw week-by-week stats using this
    league's own scoring_settings (see compute_points), counting only weeks
    Sleeper recorded them as having actually played (gp >= 1) so bye weeks
    and pre-debut rookies don't drag the average toward zero.
    """
    weekly = {}
    for week, stats_for_week in weekly_stats.items():
        stat_entry = stats_for_week.get(pid)
        if stat_entry and stat_entry.get("gp"):
            weekly[week] = compute_points(stat_entry, scoring_settings)
    if not weekly:
        return {"weekly_points": {}, "season_points": 0.0, "games_played": 0, "ppg": None, "recent_ppg": None}
    weeks_sorted = sorted(weekly)
    season_points = sum(weekly.values())
    games_played = len(weeks_sorted)
    recent_weeks = weeks_sorted[-recent_n:]
    recent_ppg = sum(weekly[w] for w in recent_weeks) / len(recent_weeks)
    return {
        "weekly_points": {w: round(weekly[w], 2) for w in weeks_sorted},
        "season_points": round(season_points, 2),
        "games_played": games_played,
        "ppg": round(season_points / games_played, 2),
        "recent_ppg": round(recent_ppg, 2),
    }


def best_bench_replacement(bench_players, slot_position, current_week):
    """Best healthy bench option for a flagged starting slot, ranked by
    actual recent scoring production (see _rank_key below) when a player
    has played enough this season to have real data, falling back to
    Sleeper's search_rank (popularity, not production) for anyone who
    doesn't yet - e.g. very early in the season. Expects `bench_players`
    to already carry the ppg/recent_ppg fields build_league_payload attaches
    from that league's own weekly scoring.
    """
    eligible = [
        p for p in bench_players
        if slot_position in (p["position"], "FLEX")
        or (slot_position == "FLEX" and p["position"] in ("RB", "WR", "TE"))
    ]
    eligible = [p for p in eligible if player_status(p, current_week) == "OK"]
    eligible.sort(key=_rank_key)
    return eligible[0] if eligible else None


# Waiver-wire suggestions only cover these positions: defenses ("DEF") are
# represented in rosters by a team abbreviation rather than a real player
# entry in Sleeper's player database, so there's no free-agent pool to rank
# them against in the first place (nothing to pick up - every NFL team's
# defense is already "out there", just not usually worth streaming weekly).
WAIVER_POSITIONS = ("QB", "RB", "WR", "TE", "K")
# Sleeper's search_rank is a popularity/notability ranking, not a
# projection - this cap just keeps "free agents" to plausibly-relevant
# players rather than every inactive/practice-squad name in the DB.
WAIVER_RANK_CAP = 600


def build_free_agents(player_lookup, rostered_ids):
    """Every fantasy-relevant player not on any roster in this league,
    cheapest (best) search_rank first. Sleeper's player database includes
    retired and currently-teamless players (search_rank and all) - "team"
    being empty is the signal that a player isn't actually on an NFL
    roster right now, so that's excluded here too, not just league
    rostering.
    """
    candidates = [
        p for p in player_lookup.values()
        if p["player_id"] not in rostered_ids
        and p["position"] in WAIVER_POSITIONS
        and p["search_rank"] < WAIVER_RANK_CAP
        and p["team"]
    ]
    candidates.sort(key=lambda p: p["search_rank"])
    return candidates


def _rank_key(p):
    """Sort key for ranking players by real production: anyone with at
    least one game of actual scoring data outranks anyone without (rookies
    who haven't debuted, players on a week-1 bye), ranked best-recent-form
    first, season PPG as the tiebreaker, and Sleeper's search_rank
    (popularity, not production) only as a last resort for players with no
    scoring data of their own yet.
    """
    has_data = p.get("ppg") is not None
    return (
        0 if has_data else 1,
        -(p.get("recent_ppg") or 0),
        -(p.get("ppg") or 0),
        p["search_rank"],
    )


def suggest_waivers(roster_players, free_agents, current_week, limit=5):
    """Up to `limit` waiver-wire targets for one team, ranked by actual
    scoring production (recent form first, season PPG as tiebreaker) rather
    than raw popularity: first, the single best available free agent at
    each position that's a real production upgrade over that team's
    weakest rostered player there (paired as an explicit drop/add), then
    backfilled with the next-best free agents overall if that leaves fewer
    than `limit`. Each suggestion carries its own BYE/injury status so a
    "hot pickup" who's on bye this week doesn't look falsely appealing.

    Early in the season (few games played) PPG is based on very little
    data and can be noisy - the has-data-first, search_rank-as-last-resort
    ordering in _rank_key means this degrades gracefully back toward the
    old popularity-based ordering when real production data is thin.
    """
    by_position = {}
    for p in roster_players:
        by_position.setdefault(p["position"], []).append(p)

    suggestions = []
    used_ids = set()

    for pos in WAIVER_POSITIONS:
        roster_at_pos = by_position.get(pos)
        if not roster_at_pos:
            continue
        weakest = max(roster_at_pos, key=_rank_key)
        for fa in sorted(free_agents, key=_rank_key):
            if fa["player_id"] in used_ids or fa["position"] != pos:
                continue
            if _rank_key(fa) < _rank_key(weakest):
                suggestions.append({
                    **fa,
                    "drop_candidate": {
                        "name": weakest["name"],
                        "ppg": weakest.get("ppg"),
                        "recent_ppg": weakest.get("recent_ppg"),
                    },
                })
                used_ids.add(fa["player_id"])
                break

    for fa in sorted(free_agents, key=_rank_key):
        if len(suggestions) >= limit:
            break
        if fa["player_id"] in used_ids:
            continue
        suggestions.append({**fa, "drop_candidate": None})
        used_ids.add(fa["player_id"])

    suggestions.sort(key=_rank_key)
    suggestions = suggestions[:limit]
    for s in suggestions:
        s["status"] = player_status(s, current_week)
    return suggestions


def build_league_payload(league_id, player_lookup, bye_weeks, current_week, weekly_stats):
    league = get(f"{API}/league/{league_id}")
    users = get(f"{API}/league/{league_id}/users")
    rosters = get(f"{API}/league/{league_id}/rosters")
    scoring_settings = league.get("scoring_settings") or {}

    # This league's own week-by-week matchups: the authoritative source for
    # every rostered player's real weekly/season score and each team's
    # weekly/season total, already computed in this league's real scoring
    # settings (see build_weekly_aggregates).
    matchups_by_week = load_league_weekly_matchups(league_id, current_week)
    player_weekly_points, team_weekly_points = build_weekly_aggregates(matchups_by_week)

    user_by_id = {u["user_id"]: u for u in users}
    roster_slots = [s for s in league["roster_positions"] if s != "BN"]

    # First pass: resolve every roster's players (attaching their scoring
    # summary from player_weekly_points) and collect every player id
    # rostered anywhere in the league, so free agents can be computed
    # league-wide before building each team's individual view.
    roster_players_by_id = {}
    all_rostered_ids = set()
    for roster in rosters:
        pids = roster.get("players") or []
        players = []
        for pid in pids:
            player = resolve_player(pid, player_lookup, bye_weeks)
            player = {**player, **rostered_player_summary(pid, player_weekly_points)}
            players.append(player)
        roster_players_by_id[roster["roster_id"]] = players
        all_rostered_ids.update(pids)

    free_agents = build_free_agents(player_lookup, all_rostered_ids)
    free_agents = [
        {**fa, **free_agent_summary(fa["player_id"], weekly_stats, scoring_settings)}
        for fa in free_agents
    ]

    teams = []
    for roster in rosters:
        owner = user_by_id.get(roster.get("owner_id"), {})
        team_name = (owner.get("metadata") or {}).get("team_name") or owner.get("display_name") or f"Team {roster['roster_id']}"

        all_players = roster_players_by_id[roster["roster_id"]]
        players_by_id = {p["player_id"]: p for p in all_players}
        starter_ids = roster.get("starters") or []
        # Look starters up from all_players (already carries the scoring
        # summary) rather than re-resolving, so starters/bench/waivers all
        # share the exact same ppg/recent_ppg numbers for a given player.
        starters_raw = [
            players_by_id.get(pid) or {**resolve_player(pid, player_lookup, bye_weeks), **rostered_player_summary(pid, player_weekly_points)}
            for pid in starter_ids
        ]
        bench = [p for p in all_players if p["player_id"] not in starter_ids]

        starters = []
        alerts = []
        for slot, player in zip(roster_slots, starters_raw):
            status = player_status(player, current_week)
            entry = {"slot": slot, **player, "status": status}
            if status in ("BYE", "OUT", "QUESTIONABLE"):
                suggestion = best_bench_replacement(bench, slot, current_week)
                entry["suggested_swap"] = suggestion
                alerts.append({
                    "slot": slot,
                    "player_name": player["name"],
                    "status": status,
                    "detail": player["injury_body_part"] if status == "QUESTIONABLE" or status == "OUT" else "Bye week",
                    "suggested_swap": suggestion["name"] if suggestion else None,
                })
            starters.append(entry)

        bench_flagged = [
            {**p, "status": player_status(p, current_week)}
            for p in bench
        ]

        team_weekly = team_weekly_points.get(roster["roster_id"], {})
        teams.append({
            "roster_id": roster["roster_id"],
            "team_name": team_name,
            "manager": owner.get("display_name"),
            "starters": starters,
            "bench": bench_flagged,
            "alert_count": len(alerts),
            "alerts": alerts,
            "waiver_suggestions": suggest_waivers(all_players, free_agents, current_week),
            "weekly_points": {w: round(pts, 2) for w, pts in sorted(team_weekly.items())},
            "season_points": round(sum(team_weekly.values()), 2),
        })

    teams.sort(key=lambda t: t["team_name"].lower())

    return {
        "league_id": league_id,
        "league_name": league.get("name"),
        "season": league.get("season"),
        "roster_slots": roster_slots,
        "teams": teams,
    }


def main():
    bye_weeks = load_bye_weeks()

    state = get(f"{API}/state/nfl")
    current_week = state.get("week") or state.get("leg") or 1

    print(f"Current NFL week: {current_week}", file=sys.stderr)
    print("Downloading full player database (this is the slow step)...", file=sys.stderr)
    players_raw = load_all_players()
    player_lookup = build_player_lookup(players_raw, bye_weeks)

    print(f"Downloading weekly stats for weeks 1-{current_week} (for free-agent scoring)...", file=sys.stderr)
    weekly_stats = load_weekly_stats(state.get("season"), current_week)

    leagues = []
    for league_id in LEAGUE_IDS:
        print(f"Processing league {league_id}...", file=sys.stderr)
        leagues.append(build_league_payload(league_id, player_lookup, bye_weeks, current_week, weekly_stats))

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_week": current_week,
        "season": state.get("season"),
        "leagues": leagues,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    total_alerts = sum(t["alert_count"] for l in leagues for t in l["teams"])
    print(f"Wrote {OUTPUT_FILE} - {len(leagues)} leagues, {total_alerts} total alerts.", file=sys.stderr)


if __name__ == "__main__":
    main()
