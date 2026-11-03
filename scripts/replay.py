"""Replay the season day by day, as if the site had been running from day 1.

For every NBA day (scoring period) from the first up to today, it rebuilds the league as it stood that
morning, before any games: that day's rosters and lineups (ESPN keeps them), each player's stats from games
played before that day only (from ESPN's game logs), and injuries judged only from games already missed
(a player who missed his NBA team's last 2 games counts as out). Then it runs the normal model and logs what
it would have said at the time:

  data/odds_history.json      - each matchup's projected margin, 80% range and win % (one point per day)
  data/prediction_log.json    - each week's Monday (first-day) forecast, marked rebuilt
  data/power_history.json     - daily power ranks (projected wins), marked rebuilt
  scripts/league_posts_history.json - daily odds/score snapshots, opening odds and weekly futures,
                                so roundups get upsets, comebacks, chokes, luck and movers

What it can't know: ESPN's injury tags and return dates at the time (only today's are kept), and ESPN's
preseason projection may have been tweaked since. Everything it writes is marked "rebuilt".
Usage: python scripts/replay.py   (safe to re-run; replaces earlier rebuilt entries, keeps live ones)
"""
import copy, json, os, sys, logging
from datetime import datetime, date, time, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

SEASON = int(S.SEASON)
MISSED_FOR_OUT = 2


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj, compact=True):
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":")) if compact else json.dump(obj, f, indent=1)


# ---------------------------------------------------------------- data from ESPN
def fetch():
    d = S._get(["mRoster", "mTeam", "mMatchup", "mMatchupScore", "mStatus", "mSettings", "mStandings"])
    latest = d["status"]["latestScoringPeriod"]
    first = d["status"].get("firstScoringPeriod") or 1
    rosters = {}
    for sp in range(first, latest + 1):
        rosters[sp] = S._get(["mRoster", "mTeam"], params={"scoringPeriodId": sp})
    ids = sorted({e["playerId"] for r in rosters.values() for t in r["teams"] for e in t["roster"]["entries"]})
    logs, prior = {}, {}
    for i in range(0, len(ids), 50):
        flt = {"players": {"filterIds": {"value": ids[i:i + 50]},
                           "filterStatsForTopScoringPeriodIds": {"value": latest + 5, "additionalValue": [
                               f"10{SEASON}", f"00{SEASON - 1}"]}}}
        for x in S._get(["kona_player_info"], headers={"X-Fantasy-Filter": json.dumps(flt)}).get("players", []):
            p = x["player"]
            g = {}
            for s in p.get("stats", []):
                if s.get("statSplitTypeId") == 5 and s.get("seasonId") == SEASON and s.get("statSourceId") == 0 \
                        and s.get("stats") and (s["stats"].get("40") or s["stats"].get("14")):
                    g[s["scoringPeriodId"]] = {"st": s["stats"], "pro": s.get("proTeamId")}
            logs[p["id"]] = g
            pr = S._avg(p, f"10{SEASON}")[0] or S._avg(p, f"00{SEASON - 1}")[0]
            prior[p["id"]] = pr
    return d, rosters, logs, prior, first, latest


# ---------------------------------------------------------------- the model, as of the morning of day `sp`
class AsOf:
    def __init__(self, logs, prior, sched, sp_date):
        self.logs, self.prior, self.sched, self.sp_date = logs, prior, sched, sp_date
        self.sp = None

    def rates(self, p):
        pid = p["id"]
        games = [g["st"] for s, g in self.logs.get(pid, {}).items() if s < self.sp]
        gp = len(games)
        cur = {k: sum(float(g.get(k, 0) or 0) for g in games) / gp for k in S.KEYS} if gp else None
        prior = self.prior.get(pid)
        if not prior and not cur:
            return None
        if not prior:
            prior = S.REPLACEMENT
        w = gp / (gp + 15) if cur else 0
        return {k: w * (cur or {}).get(k, 0) + (1 - w) * prior.get(k, 0) for k in S.KEYS}

    def status(self, p):
        """OUT if he missed his NBA team's last MISSED_FOR_OUT games before this day."""
        pro = p.get("proTeamId") or 0
        if not pro:
            return "OUT"
        day = self.sp_date(self.sp)
        team_days = sorted({g.date() for g in self.sched.get(pro, []) if g.date() < day})[-MISSED_FOR_OUT:]
        if len(team_days) < MISSED_FOR_OUT:
            return "ACTIVE"
        played = {self.sp_date(s) for s in self.logs.get(p["id"], {})}
        return "OUT" if not any(td in played for td in team_days) else "ACTIVE"

    def install(self):
        me = self

        def pinfo(p, slot=None):
            return {"id": p["id"], "name": p.get("fullName"), "pro": p.get("proTeamId", 0),
                    "elig": [s for s in p.get("eligibleSlots", []) if s in (0, 1, 2, 3, 4, 5, 6, 11)],
                    "status": me.status(p), "rates": me.rates(p), "slot": slot, "ret": None}
        S._pinfo = pinfo
        S._injury_returns = lambda: {}


def league_asof(d_now, roster_view, week, latest_week_done_sp, cur_sp):
    """Copy of the league with that morning's rosters, records from finished weeks only, and every
    matchup from `week` on undecided."""
    d = copy.deepcopy(d_now)
    teams = {t["id"]: t for t in roster_view["teams"]}
    for t in d["teams"]:
        t["roster"] = teams[t["id"]]["roster"]
        t["record"]["overall"].update(wins=0, losses=0, ties=0)
    by_id = {t["id"]: t for t in d["teams"]}
    for m in d["schedule"]:
        if "away" not in m:
            continue
        if m["matchupPeriodId"] >= week:
            m["winner"] = "UNDECIDED"
            continue
        h, a = by_id[m["home"]["teamId"]]["record"]["overall"], by_id[m["away"]["teamId"]]["record"]["overall"]
        if m["winner"] == "HOME":
            h["wins"] += 1; a["losses"] += 1
        elif m["winner"] == "AWAY":
            a["wins"] += 1; h["losses"] += 1
        elif m["winner"] == "TIE":
            h["ties"] += 1; a["ties"] += 1
    d["status"] = dict(d["status"], currentMatchupPeriod=week, latestScoringPeriod=cur_sp)
    return d


def main():
    d_now, rosters, logs, prior, first, latest = fetch()
    names = {t["id"]: t["name"] for t in d_now["teams"]}
    sched = S._schedule()
    # scoring period -> US Eastern date (one NBA day per scoring period)
    sp_date = S._sp_date
    # weeks: Monday-Sunday; week 1 starts on opening day
    week_of = {}
    w, prev = 1, None
    for sp in range(first, latest + 8):
        dt = sp_date(sp)
        if prev is not None and dt.weekday() == 0:
            w += 1
        week_of[sp] = w
        prev = dt
    reg = d_now["settings"]["scheduleSettings"]["matchupPeriodCount"]
    days_cache = load(os.path.join(ROOT, "data", "player_days.json"), {})
    A = AsOf(logs, prior, sched, sp_date)
    A.install()

    oh = load(os.path.join(ROOT, "data", "odds_history.json"), {})
    plog = load(os.path.join(ROOT, "data", "prediction_log.json"), {"forecasts": {}, "results": {}})
    ph = load(os.path.join(ROOT, "data", "power_history.json"), {"days": []})
    hist = S._posts_load()
    # drop earlier rebuilt entries so the replay can be re-run
    for wk in oh.values():
        for k in list(wk):
            wk[k] = [p for p in wk[k] if not (len(p) > 7 and p[7] == "r")]
    ph["days"] = [x for x in ph["days"] if not x.get("rebuilt")]
    live_dates = {x["date"] for x in ph["days"]}

    for sp in range(first, latest + 1):
        A.sp = sp
        week = week_of[sp]
        if week > reg:
            break
        today = sp_date(sp)
        days = [sp_date(s) for s in range(sp, latest + 8) if week_of[s] == week]
        # points already scored this week (starters only), from the day-by-day lineup cache
        pts = {t: 0.0 for t in names}
        for s in range(first, sp):
            if week_of[s] != week:
                continue
            for pid, x in (days_cache.get(str(s), {}).get("p") or {}).items():
                if x[1]:
                    pts[x[0]] += x[2]
        dA = league_asof(d_now, rosters[sp], week, None, sp)
        ts = datetime.combine(today, time(11, 0), S.ET).astimezone(timezone.utc).isoformat(timespec="seconds")
        ts = ts.replace("+00:00", "")[:19] + "Z"
        live, snap_odds, snap_scores, fc = {}, {}, {}, {}
        for m in dA["schedule"]:
            if m["matchupPeriodId"] != week or "away" not in m:
                continue
            h, a = m["home"]["teamId"], m["away"]["teamId"]
            _, ph_ = S._team_players(h, dA)
            _, pa_ = S._team_players(a, dA)
            r = S._matchup_odds(ph_, pa_, days, sched, {"FP": pts[h]}, {"FP": pts[a]}, n=1000, now=None)
            p_h, tie = r["win_prob"], r["tie_prob"]
            live[(h, a)] = (p_h, tie)
            mk = S._h2h_market(p_h, tie)
            margin = round(r["proj_points"]["me"] - r["proj_points"]["opp"], 1)
            lo, hi = r["margin_range_80pct"]
            oh.setdefault(str(week), {}).setdefault(f"{names[h]}|{names[a]}", []).append(
                [ts, margin, lo, hi, round(100 * p_h), round(pts[h], 1), round(pts[a], 1), "r"])
            snap_odds[f"{h}-{a}"] = {"ph": p_h, "pt": tie, "price_home": mk["home"], "price_away": mk["away"]}
            snap_scores[f"{h}-{a}"] = [round(pts[h], 1), round(pts[a], 1)]
            fc[f"{h}-{a}"] = {"home": names[h], "away": names[a], "p_home": p_h, "p_tie": tie,
                              "proj": [r["proj_points"]["me"], r["proj_points"]["opp"]], "margin_80": [lo, hi]}
        # season + finals simulation as of this morning
        P, power = S._pairwise(dA)
        sim, po_n, _ = S._season_sim(dA, P, live=live, n=10000)
        order = sorted(names, key=lambda t: -sim[t]["exp_wins"])
        if today.isoformat() not in live_dates:
            ph["days"].append({"date": today.isoformat(), "week": week, "rebuilt": True,
                               "ranks": {names[t]: i + 1 for i, t in enumerate(order)},
                               "proj_w": {names[t]: round(sim[t]["exp_wins"], 1) for t in names},
                               "title": {names[t]: round(100 * sim[t]["title"], 1) for t in names},
                               "finals": {names[t]: round(100 * sim[t]["finals"], 1) for t in names}})
        first_day_of_week = sp == first or week_of[sp - 1] != week
        if first_day_of_week:
            plog["forecasts"][str(week)] = {"saved_utc": ts, "opening": True, "rebuilt": True, "matchups": fc}
            hist["opening"][str(week)] = snap_odds
            if week > 1:  # futures as the previous week closed, for the weekly roundup's movers
                ids = list(sim)
                tp = S._book([sim[t]["title"] for t in ids], S.FUTURES_MARGIN)
                fp = S._book([sim[t]["finals"] for t in ids], S.FUTURES_MARGIN, places=po_n)
                hist["weekly"][str(week - 1)] = {
                    "saved": ts, "rebuilt": True,
                    "title_prices": {str(t): p for t, p in zip(ids, tp)},
                    "title_pct": {str(t): sim[t]["title"] for t in ids},
                    "finals_prices": {str(t): p for t, p in zip(ids, fp)},
                    "power_ranks": {str(t): i + 1 for i, t in enumerate(order)},
                    "power_pct": {str(t): round(power[t] * 100) for t in order}}
        key = today.isoformat()
        if key not in hist["daily"] or hist["daily"][key].get("rebuilt"):
            hist["daily"][key] = {"mp": week, "sp": sp, "saved": ts, "rebuilt": True,
                                  "odds": snap_odds, "scores": snap_scores, "injuries": {}}
        print(f"day {sp} ({today}, week {week}): {len(fc)} matchups, top power: {names[order[0]]}")

    for wk in oh.values():
        for k in wk:
            wk[k].sort(key=lambda p: p[0])
    ph["days"].sort(key=lambda x: x["date"])
    hist["daily"] = dict(sorted(hist["daily"].items())[-120:])
    save(os.path.join(ROOT, "data", "odds_history.json"), oh)
    save(os.path.join(ROOT, "data", "prediction_log.json"), plog)
    save(os.path.join(ROOT, "data", "power_history.json"), ph)
    S._posts_save(hist)
    print("replay done")


if __name__ == "__main__":
    main()
