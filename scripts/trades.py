"""Trade tracker for a POINTS league: every trade this season and how it's turning out.
Writes data/trades.json.

For each trade:
  gets      - who each team received
  so_far    - fantasy points each team's incoming players have scored IN ITS STARTING LINEUP from the
              trade day on, on days ESPN's roster shows him on that team (bench games don't count; a player stops counting once he leaves that team).
              Uneven trades (2-for-1 etc.): the team receiving fewer players frees a roster spot, so it is
              credited with an average free agent in each freed spot (FA points per game x the share of
              days an NBA team plays). Day-by-day running margin for the chart.
  at_time   - the model's change in expected wins (rest of season) for each side when first seen
  now       - the same re-run with today's rosters and injuries (while every player is still where he went)

Trades are found in ESPN's transaction log (mTransactions2), scanned once for the whole season and
then only for recent days. Every NBA day's lineups are cached in data/player_days.json.
Run after live.py.
"""
import json, os, sys, logging
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

DAYS = os.path.join(ROOT, "data", "player_days.json")
LOG = os.path.join(ROOT, "data", "trades_log.json")
OUT = os.path.join(ROOT, "data", "trades.json")
TEAM_PLAY_SHARE = 0.48   # an NBA team plays on roughly 48% of the season's days
FA_POOL = 10             # "average free agent" = mean of the best 10 available by fantasy points per game


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def player_days(d):
    """{sp: {"date": "YYYY-MM-DD", "p": {player_id: [team_id, started, fpts]}}} for every finished day."""
    cache = load(DAYS, {})
    for sp, v in cache.items():  # (re)date cached days from opening night
        v["date"] = str(S._sp_date(int(sp)))
    latest = d["status"].get("latestScoringPeriod") or 1
    first = d["status"].get("firstScoringPeriod") or 1
    today = datetime.now(S.ET).date()
    for sp in range(first, latest):
        if str(sp) in cache:
            continue
        try:
            dd = S._get(["mRoster", "mTeam"], params={"scoringPeriodId": sp})
        except Exception:
            continue
        p = {}
        for t in dd["teams"]:
            for e in t["roster"]["entries"]:
                pl = e["playerPoolEntry"]["player"]
                st = S._day_line(pl, sp)
                if st:
                    p[str(pl["id"])] = [t["id"], e["lineupSlotId"] not in (12, 13), round(S._fp(st), 1)]
        cache[str(sp)] = {"date": str(S._sp_date(sp)), "p": p}
    with open(DAYS, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    return cache


def scan_trades(d, log):
    """Find executed trades in ESPN's transaction log. Full-season scan the first time, then recent days."""
    latest = d["status"].get("transactionScoringPeriod") or d["status"].get("latestScoringPeriod") or 1
    known = log.setdefault("_trades", {})
    adds = log.setdefault("_adds", {})
    start = 0 if not log.get("_scanned_full_v2") else max(0, latest - 4)
    for sp in range(start, latest + 2):
        try:
            tx = S._get(["mTransactions2"], params={"scoringPeriodId": sp}).get("transactions", [])
        except Exception:
            continue
        for t in tx:
            when = t.get("processDate") or t.get("proposedDate")  # free-agent adds only carry proposedDate
            if t.get("type") in ("FREEAGENT", "WAIVER") and t.get("status") == "EXECUTED" and when:
                for i in t.get("items", []):
                    if i.get("type") == "DROP" and i.get("playerId"):
                        log.setdefault("_drops", {}).setdefault(f'{t["id"]}:{i["playerId"]}', {
                            "team": i.get("fromTeamId") or t.get("teamId"), "player": i["playerId"],
                            "date_utc": datetime.fromtimestamp(when / 1000, timezone.utc).isoformat()})
                    if i.get("type") == "ADD" and i.get("playerId"):
                        adds.setdefault(f'{t["id"]}:{i["playerId"]}', {
                            "team": i.get("toTeamId") or t.get("teamId"), "player": i["playerId"],
                            "bid": t.get("bidAmount") or 0, "type": t["type"],
                            "date_utc": datetime.fromtimestamp(when / 1000, timezone.utc).isoformat()})
            if t.get("type") not in ("TRADE_ACCEPT", "TRADE_UPHOLD", "TRADE_PROPOSAL") or t.get("status") != "EXECUTED":
                continue
            items = [i for i in t.get("items", []) if i.get("playerId") and i.get("toTeamId")]
            if not items or not t.get("processDate"):
                continue  # accepted but never processed (vetoed / cancelled) or no player list
            key = t["id"]
            if key in known:
                continue
            gets = {}
            for i in items:
                gets.setdefault(str(i["toTeamId"]), []).append(i["playerId"])
            if len(gets) != 2:
                continue
            known[key] = {"gets": gets,
                          "accepted_utc": datetime.fromtimestamp((t.get("proposedDate") or t["processDate"]) / 1000, timezone.utc).isoformat(),
                          "processed_utc": datetime.fromtimestamp(t["processDate"] / 1000, timezone.utc).isoformat()}
    log["_scanned_full"] = log["_scanned_full_v2"] = True
    return known


def infer_from_rosters(days, known, log):
    """Trades ESPN logs without a player list: players moving straight from one fantasy team to another on
    the same day, in both directions, between the same two teams."""
    adds = log.get("_adds", {}).values()
    prev, moves = {}, {}
    for sp in sorted(days, key=int):
        date = days[sp]["date"]
        for pid, x in days[sp]["p"].items():
            if pid in prev and prev[pid][0] != x[0]:
                a, b = prev[pid][0], x[0]
                claimed = any(ad["player"] == int(pid) and ad["team"] == b and abs(
                    (datetime.fromisoformat(ad["date_utc"]).astimezone(S.ET).date() - datetime.fromisoformat(date).date()).days) <= 3
                    for ad in adds)
                if not claimed:
                    moves.setdefault((date, frozenset((a, b))), []).append((int(pid), a, b))
            prev[pid] = x
    have = {frozenset(p for lst in t["gets"].values() for p in lst) for t in known.values()}
    for (date, pair), mv in moves.items():
        if len(pair) != 2 or len({a for _, a, _ in mv}) != 2:
            continue  # need players going both ways
        gets = {}
        for pid, a, b in mv:
            gets.setdefault(str(b), []).append(pid)
        if frozenset(p for _, p, _ in [(0, pid, 0) for pid, _, _ in mv]) in have:
            continue
        stamp = datetime.combine(datetime.fromisoformat(date).date(), datetime.min.time(), S.ET).astimezone(timezone.utc).isoformat()
        known[f"roster:{date}:{'-'.join(sorted(str(p) for p, _, _ in mv))}"] = {
            "gets": gets, "accepted_utc": stamp, "processed_utc": stamp, "source": "daily rosters"}


def fa_level():
    """Average free agent's fantasy points per game (best FA_POOL available, 3+ games or a projection)."""
    vals = []
    for x in S._pool():
        if x.get("onTeamId", 0) != 0:
            continue
        p = x["player"]
        if not p.get("proTeamId"):
            continue
        r = S._rates(p)
        if r and (p.get("injuryStatus") or "ACTIVE") == "ACTIVE":
            vals.append(S._fp(r))
    vals.sort(reverse=True)
    top = vals[:FA_POOL]
    return round(sum(top) / len(top), 1) if top else 0.0


def replacement(fpg):
    """A synthetic 'average free agent' player for the season model."""
    scale = fpg / max(1e-9, S._fp(S.REPLACEMENT))
    return {"id": -1, "name": "Average free agent", "pro": 1, "elig": [0, 1, 2, 3, 4, 5, 6, 11], "status": "ACTIVE",
            "rates": {k: v * scale for k, v in S.REPLACEMENT.items()}, "slot": 12, "ret": None}


def model_change(d, rosters, side, other, gets, fpg):
    """Change in `side`'s expected wins (rest of season) from the trade, using current rosters.
    None if the players aren't all where the trade sent them (or where they started)."""
    got = set(gets[str(side)])
    gave = set(gets[str(other)])
    mine = {p["id"]: p for p in rosters[side]}
    if got <= set(mine):            # already processed: compare with the pre-trade roster
        after = rosters[side]
        before = [p for p in after if p["id"] not in got] + [p for p in rosters[other] if p["id"] in gave]
        if len(before) != len(after) - len(got) + len(gave):
            return None
    elif gave <= set(mine):         # not processed yet
        before = rosters[side]
        after = [p for p in before if p["id"] not in gave] + [p for p in rosters[other] if p["id"] in got]
    else:
        return None
    for _ in range(max(0, len(before) - len(after))):   # freed roster spot -> average free agent
        after = after + [replacement(fpg)]
    for _ in range(max(0, len(after) - len(before))):
        before = before + [replacement(fpg)]
    wk = S._remaining_weeks(d)
    av, _ = S._fixture_value(after, d, side, wk)
    bv, _ = S._fixture_value(before, d, side, wk)
    wpts = S._weekly_dist(after)[0]["FP"] - S._weekly_dist(before)[0]["FP"]
    return {"exp_wins": round((av - bv) * S.GAIN_CALIBRATION, 2), "weekly_points": round(wpts, 1)}


def pickups(log, days, names, pool_names):
    """Every waiver / free-agent pickup this season and what he has given the team that added him:
    fantasy points in its starting lineup from the day after the add until he leaves."""
    out = []
    merged = {}  # a player dropped and re-added by the same team counts once, from his first add
    for a in log.get("_adds", {}).values():
        k = (a["team"], a["player"])
        if k not in merged or a["date_utc"] < merged[k]["date_utc"]:
            merged[k] = dict(a, bid=max(a["bid"], merged.get(k, {}).get("bid", 0)))
    for a in merged.values():
        tid, pid = a["team"], str(a["player"])
        if tid not in names:
            continue
        # from the day of the add (US date): each day's ESPN roster says whether he was really on the team
        first = str(datetime.fromisoformat(a["date_utc"]).astimezone(S.ET).date())
        starts = fpts = 0.0
        held = 0
        for sp in sorted(days, key=int):
            day = days[sp]
            if day["date"] < first:
                continue
            x = day["p"].get(pid)
            if x and x[0] == tid:
                held += 1
                if x[1]:
                    starts += 1
                    fpts += x[2]
        out.append({"player": pool_names.get(a["player"], pid), "team": names[tid], "bid": a["bid"],
                    "type": "waiver claim" if a["type"] == "WAIVER" else "free agent",
                    "date": first, "starts": int(starts), "fpts": round(fpts, 1),
                    "per_start": round(fpts / starts, 1) if starts else None})
    out.sort(key=lambda x: -x["fpts"])
    best_ids = {(x["player"], x["team"]) for x in out[:10]}
    worst = sorted([x for x in out if x["starts"] >= 3 and (x["player"], x["team"]) not in best_ids], key=lambda x: x["per_start"])
    return {"count": len(out), "best": out[:10], "worst": worst[:10], "all": out,
            "by_team": sorted(({"team": t, "pickups": sum(1 for x in out if x["team"] == t),
                                "fpts": round(sum(x["fpts"] for x in out if x["team"] == t), 1),
                                "faab": sum(x["bid"] for x in out if x["team"] == t)} for t in names.values()),
                              key=lambda r: -r["fpts"])}


def drops(log, names, pool):
    """Every drop this season and what the player has done since, in all his NBA games (wherever he is
    now): fantasy points from the first game after the drop. A player the same team later re-added
    doesn't count as a drop."""
    adds = log.get("_adds", {}).values()
    latest = {}
    for x in log.get("_drops", {}).values():
        k = (x["team"], x["player"])
        if k not in latest or x["date_utc"] > latest[k]["date_utc"]:
            latest[k] = x
    keep = [x for (tid, pid), x in latest.items() if tid in names and not any(
        a["team"] == tid and a["player"] == pid and a["date_utc"] > x["date_utc"] for a in adds)]
    ids = sorted({x["player"] for x in keep})
    logs = {}
    for i in range(0, len(ids), 50):
        flt = {"players": {"filterIds": {"value": ids[i:i + 50]},
                           "filterStatsForTopScoringPeriodIds": {"value": 200, "additionalValue": []}}}
        try:
            got = S._get(["kona_player_info"], headers={"X-Fantasy-Filter": json.dumps(flt)}).get("players", [])
        except Exception:
            got = []
        for x in got:
            logs[x["player"]["id"]] = [(S._sp_date(s["scoringPeriodId"]), S._fp(s["stats"]))
                                       for s in x["player"].get("stats", [])
                                       if s.get("statSplitTypeId") == 5 and s.get("seasonId") == int(S.SEASON)
                                       and s.get("statSourceId") == 0 and s.get("stats") and s["stats"].get("40")]
    out = []
    for x in keep:
        t = datetime.fromisoformat(x["date_utc"]).astimezone(S.ET)
        first = t.date() if t.hour < 19 else t.date() + timedelta(days=1)  # dropped before tip-off counts that day
        g = [fp for dt, fp in logs.get(x["player"], []) if dt >= first]
        p = pool.get(x["player"], {})
        out.append({"player": p.get("player", {}).get("fullName", str(x["player"])), "team": names[x["team"]],
                    "now": names.get(p.get("onTeamId"), "free agent"), "date": str(t.date()),
                    "games": len(g), "fpts": round(sum(g), 1), "per_game": round(sum(g) / len(g), 1) if g else None})
    out.sort(key=lambda r: -r["fpts"])
    return {"count": len(out), "all": out}


def grade(margin_per_week):
    """Letter grade from fantasy points per week gained vs the other side."""
    for lim, g in ((60, "A+"), (40, "A"), (25, "A-"), (12, "B+"), (4, "B"), (-4, "C"), (-12, "C-"),
                   (-25, "D+"), (-40, "D"), (-60, "D-")):
        if margin_per_week >= lim:
            return g
    return "F"


def main():
    d = S._get(["mRoster", "mTeam", "mMatchup", "mSettings", "mStatus"])
    names = {t["id"]: t.get("name").strip() for t in d["teams"]}
    rosters = {t["id"]: S._team_players(t["id"], d)[1] for t in d["teams"]}
    pool_names = {x["player"]["id"]: x["player"].get("fullName") for x in S._pool()}
    days = player_days(d)
    log = load(LOG, {})
    known = scan_trades(d, log)
    infer_from_rosters(days, known, log)
    fpg = fa_level()
    fa_day = round(fpg * TEAM_PLAY_SHARE, 2)
    today = str(datetime.now(S.ET).date())
    out = []
    for key, t in known.items():
        A, B = sorted((int(x) for x in t["gets"]), key=lambda x: names[x])
        gets = t["gets"]
        rec = log.setdefault(key, {"first_seen": today, "history": []})
        if "at_time" not in rec:
            at = {names[s]: model_change(d, rosters, s, o, gets, fpg) for s, o in ((A, B), (B, A))}
            if all(at.values()):
                rec["at_time"] = at
                rec["at_time_source"] = "model when first seen" if rec["first_seen"] <= t["processed_utc"][:10] \
                    else f"model on {rec['first_seen']} (trade made before the tracker started)"
        # ---- so far: day by day from the day after processing
        first_day = str(datetime.fromisoformat(t["processed_utc"]).astimezone(S.ET).date())  # roster check per day
        open_spots = {A: max(0, len(gets[str(B)]) - len(gets[str(A)])), B: max(0, len(gets[str(A)]) - len(gets[str(B)]))}
        tot = {A: 0.0, B: 0.0}
        games = {A: 0, B: 0}
        players = {s: {pid: {"name": pool_names.get(pid, str(pid)), "games": 0, "fpts": 0.0} for pid in gets[str(s)]} for s in (A, B)}
        series = []
        for sp in sorted(days, key=int):
            day = days[sp]
            if day["date"] < first_day:
                continue
            for s in (A, B):
                for pid in gets[str(s)]:
                    x = day["p"].get(str(pid))
                    if x and x[0] == s and x[1]:
                        tot[s] += x[2]
                        games[s] += 1
                        players[s][pid]["games"] += 1
                        players[s][pid]["fpts"] += x[2]
                tot[s] += open_spots[s] * fa_day
            series.append([day["date"], round(tot[A] - tot[B], 1)])
        ndays = len(series)
        wk = ndays / 7
        margin = tot[A] - tot[B]
        so_far = {"from": first_day, "days": ndays,
                  "points": {names[A]: round(tot[A], 1), names[B]: round(tot[B], 1)},
                  "margin": {names[A]: round(margin, 1), names[B]: round(-margin, 1)},
                  "per_week": {names[A]: round(margin / wk, 1) if wk else 0, names[B]: round(-margin / wk, 1) if wk else 0},
                  "games": {names[A]: games[A], names[B]: games[B]},
                  "fa_credit": {names[s]: round(open_spots[s] * fa_day * ndays, 1) for s in (A, B) if open_spots[s]},
                  "players": {names[s]: [{"name": v["name"], "games": v["games"], "fpts": round(v["fpts"], 1)}
                                         for v in players[s].values()] for s in (A, B)},
                  "series": series,
                  "grade": {names[A]: grade(margin / wk) if wk >= 1 else None,
                            names[B]: grade(-margin / wk) if wk >= 1 else None}}
        nv = {names[s]: model_change(d, rosters, s, o, gets, fpg) for s, o in ((A, B), (B, A))}
        frozen = not all(nv.values())
        if not frozen:
            rec["now"] = {"date": today, **nv}
            h = [today, nv[names[A]]["exp_wins"], nv[names[B]]["exp_wins"]]
            if rec["history"] and rec["history"][-1][0] == today:
                rec["history"][-1] = h
            else:
                rec["history"].append(h)
        out.append({"key": key, "accepted_utc": t["accepted_utc"], "processed_utc": t["processed_utc"],
                    "teams": [names[A], names[B]],
                    "gets": {names[s]: [pool_names.get(pid, str(pid)) for pid in gets[str(s)]] for s in (A, B)},
                    "at_time": rec.get("at_time"), "at_time_source": rec.get("at_time_source"),
                    "post": rec.get("post"), "so_far": so_far,
                    "now": rec.get("now"), "now_frozen": frozen, "now_history": rec["history"]})
    out.sort(key=lambda r: r["processed_utc"], reverse=True)
    with open(LOG, "w") as f:
        json.dump(log, f, indent=1)
    with open(OUT, "w") as f:
        json.dump({"updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "fa_points_per_game": fpg, "fa_points_per_day": fa_day, "fa_pool": FA_POOL,
                   "trades": out, "pickups": pickups(log, days, names, pool_names),
                   "drops": drops(log, names, {x["player"]["id"]: x for x in S._pool()})}, f, indent=1)
    print(f"trades: {len(out)}, days cached: {len(days)}, avg FA {fpg} FPTS/game")


if __name__ == "__main__":
    main()
