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


def best_bench_replacement(bench_players, slot_position, current_week):
    """Very rough placeholder ranking: Sleeper's own search_rank (overall
    popularity, not a projection). Swap this for real weekly rankings
    (e.g. a FantasyPros CSV export) by replacing this function - everything
    downstream just expects it to return the best candidate first.
    """
    eligible = [
        p for p in bench_players
        if slot_position in (p["position"], "FLEX")
        or (slot_position == "FLEX" and p["position"] in ("RB", "WR", "TE"))
    ]
    eligible = [p for p in eligible if player_status(p, current_week) == "OK"]
    eligible.sort(key=lambda p: p["search_rank"])
    return eligible[0] if eligible else None


# Waiver-wire suggestions only cover these positions: defenses ("DEF") are
# represented in rosters by a team abbreviation rather than a real player
# entry, so we have no search_rank (or any other signal) to rank them by -
# same placeholder-ranking caveat as best_bench_replacement above.
WAIVER_POSITIONS = ("QB", "RB", "WR", "TE", "K")
# Sleeper's search_rank is a popularity/notability ranking, not a
# projection - this cap just keeps "free agents" to plausibly-relevant
# players rather than every inactive/practice-squad name in the DB.
WAIVER_RANK_CAP = 600


def build_free_agents(player_lookup, rostered_ids):
    """Every fantasy-relevant player not on any roster in this league,
    cheapest (best) search_rank first."""
    candidates = [
        p for p in player_lookup.values()
        if p["player_id"] not in rostered_ids
        and p["position"] in WAIVER_POSITIONS
        and p["search_rank"] < WAIVER_RANK_CAP
    ]
    candidates.sort(key=lambda p: p["search_rank"])
    return candidates


def suggest_waivers(roster_players, free_agents, current_week, limit=5):
    """Up to `limit` waiver-wire targets for one team: first, the single
    best available free agent at each position that's a clear upgrade over
    that team's weakest rostered player there; then backfilled with the
    next-best free agents overall (regardless of position) if that leaves
    fewer than `limit`. Each suggestion carries its own BYE/injury status so
    a "hot pickup" who's on bye this week doesn't look falsely appealing.
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
        weakest = max(roster_at_pos, key=lambda p: p["search_rank"])
        for fa in free_agents:
            if fa["player_id"] in used_ids or fa["position"] != pos:
                continue
            if fa["search_rank"] < weakest["search_rank"]:
                suggestions.append({**fa, "upgrade_over": weakest["name"]})
                used_ids.add(fa["player_id"])
                break

    for fa in free_agents:
        if len(suggestions) >= limit:
            break
        if fa["player_id"] in used_ids:
            continue
        suggestions.append({**fa, "upgrade_over": None})
        used_ids.add(fa["player_id"])

    suggestions.sort(key=lambda p: p["search_rank"])
    suggestions = suggestions[:limit]
    for s in suggestions:
        s["status"] = player_status(s, current_week)
    return suggestions


def build_league_payload(league_id, player_lookup, bye_weeks, current_week):
    league = get(f"{API}/league/{league_id}")
    users = get(f"{API}/league/{league_id}/users")
    rosters = get(f"{API}/league/{league_id}/rosters")

    user_by_id = {u["user_id"]: u for u in users}
    roster_slots = [s for s in league["roster_positions"] if s != "BN"]

    # First pass: resolve every roster's players and collect every player
    # id rostered anywhere in the league, so free agents can be computed
    # league-wide before building each team's individual view.
    roster_players_by_id = {}
    all_rostered_ids = set()
    for roster in rosters:
        pids = roster.get("players") or []
        roster_players_by_id[roster["roster_id"]] = [
            resolve_player(pid, player_lookup, bye_weeks) for pid in pids
        ]
        all_rostered_ids.update(pids)

    free_agents = build_free_agents(player_lookup, all_rostered_ids)

    teams = []
    for roster in rosters:
        owner = user_by_id.get(roster.get("owner_id"), {})
        team_name = (owner.get("metadata") or {}).get("team_name") or owner.get("display_name") or f"Team {roster['roster_id']}"

        all_players = roster_players_by_id[roster["roster_id"]]
        starter_ids = roster.get("starters") or []
        starters_raw = [resolve_player(pid, player_lookup, bye_weeks) for pid in starter_ids]
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

        teams.append({
            "roster_id": roster["roster_id"],
            "team_name": team_name,
            "manager": owner.get("display_name"),
            "starters": starters,
            "bench": bench_flagged,
            "alert_count": len(alerts),
            "alerts": alerts,
            "waiver_suggestions": suggest_waivers(all_players, free_agents, current_week),
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

    leagues = []
    for league_id in LEAGUE_IDS:
        print(f"Processing league {league_id}...", file=sys.stderr)
        leagues.append(build_league_payload(league_id, player_lookup, bye_weeks, current_week))

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
