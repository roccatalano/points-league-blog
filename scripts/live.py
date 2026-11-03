"""Refresh the site's live data (no posts, no AI). Writes:
  data/odds.json   - every current matchup: points so far, win chance, projected final score,
                     projected margin with an 80% range, bookmaker prices
  data/league.json - ladder: record, points for/against, power rank, projected record, title/finals odds
  data/model.json  - rosters + remaining fixtures, used by the trade calculator in the browser
The league is public, so no cookies are needed (ESPN_S2 / ESPN_SWID are used if set).
"""
import json, os, sys, logging
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

LIVE = {}


def odds(d, names):
    sched = S._schedule()
    now = datetime.now(S.ET)
    days = S._week_days(0)
    mp = d["status"].get("currentMatchupPeriod")
    out = []
    for m in d["schedule"]:
        if m["matchupPeriodId"] != mp or not m.get("away"):
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        _, _, base_h, base_a = S._current_matchup(d, h)
        _, ph = S._team_players(h, d)
        _, pa = S._team_players(a, d)
        res = S._matchup_odds(ph, pa, days, sched, base_h, base_a, n=1000, now=now)
        p_h, tie = res["win_prob"], res["tie_prob"]
        LIVE[(h, a)] = (p_h, tie)
        p_a = max(0.0, 1 - p_h - tie)
        mk = S._h2h_market(p_h, tie)
        proj_h, proj_a = res["proj_points"]["me"], res["proj_points"]["opp"]

        def games_left(players):
            total = 0
            for day in days:
                playing = [p for p in players if p["rates"] and p["slot"] != 13 and S._play_prob(p, day) > 0
                           and S._games_on(p["pro"], day, sched, now if day == now.date() else None)]
                total += len(S._daily_lineup(playing))
            return total

        out.append({"home": names[h], "away": names[a], "home_id": h, "away_id": a,
                    "points_now": [round(base_h["FP"], 1), round(base_a["FP"], 1)],
                    "proj_final": [proj_h, proj_a],
                    "proj_margin": round(proj_h - proj_a, 1),           # + = home ahead
                    "margin_80": res["margin_range_80pct"],             # [p10, p90] of home - away
                    "games_left": [games_left(ph), games_left(pa)],
                    "home_win_pct": round(100 * p_h), "away_win_pct": round(100 * p_a), "tie_pct": round(100 * tie),
                    "price_home": mk["home"], "price_away": mk["away"]})
    out.sort(key=lambda x: abs(x["home_win_pct"] - x["away_win_pct"]))
    return {"matchup_period": mp, "days_left_US": [str(x) for x in days], "matchups": out}


def league(d, names, live_pairs):
    """Ladder + premiership / make-finals odds from the season simulator (same as the weekly post)."""
    P, power = S._pairwise(d)
    sim, po_n, in_playoffs = S._season_sim(d, P, live=live_pairs)
    ids = [t["id"] for t in d["teams"]]
    title_prices = S._book([sim[t]["title"] for t in ids], S.FUTURES_MARGIN, places=1)
    finals_prices = S._book([sim[t]["finals"] for t in ids], S.FUTURES_MARGIN, places=po_n)
    pf = {t: 0.0 for t in ids}
    pa = {t: 0.0 for t in ids}
    played = {t: 0 for t in ids}
    reg = d["settings"]["scheduleSettings"]["matchupPeriodCount"]
    for m in d["schedule"]:
        if "away" not in m or m.get("winner") in (None, "UNDECIDED") or m["matchupPeriodId"] > reg:
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        hp, ap = S._side_points(m["home"], live=False), S._side_points(m["away"], live=False)
        pf[h] += hp; pa[h] += ap; played[h] += 1
        pf[a] += ap; pa[a] += hp; played[a] += 1
    dists = {t: S._weekly_dist(S._team_players(t, d)[1]) for t in ids}
    order = sorted(ids, key=lambda t: -power[t])
    rows = []
    for i, t in enumerate(d["teams"]):
        tid = t["id"]
        r = t["record"]["overall"]
        rows.append({"team": names[tid], "seed": t.get("playoffSeed"),
                     "w": r["wins"], "l": r["losses"], "t": r["ties"],
                     "pf": round(pf[tid], 1), "pa": round(pa[tid], 1),
                     "pf_avg": round(pf[tid] / played[tid], 1) if played[tid] else None,
                     "proj_week": round(dists[tid][0]["FP"]),  # typical full week, season model
                     "faab_left": 500 - (t.get("transactionCounter", {}) or {}).get("acquisitionBudgetSpent", 0),
                     "power_pct": round(100 * power[tid]), "power_rank": order.index(tid) + 1,
                     "proj_w": round(sim[tid]["exp_wins"], 1), "proj_l": round(reg - sim[tid]["exp_wins"], 1),
                     "finals_pct": round(100 * sim[tid]["finals"], 1),
                     "title_pct": round(100 * sim[tid]["title"], 1),
                     "title_price": title_prices[i], "finals_price": finals_prices[i]})
    rows.sort(key=lambda x: (x["seed"] or 99))
    return {"finals_teams": po_n, "in_playoffs": in_playoffs, "sims": S.SEASON_SIMS, "weeks": reg, "ladder": rows}


def model(d, names):
    from trades import fa_level
    fa_fpg = fa_level()
    weeks = S._remaining_weeks(d)
    teams = []
    for t in d["teams"]:
        _, pl = S._team_players(t["id"], d)
        teams.append({"id": t["id"], "name": names[t["id"]],
                      "players": [{"name": p["name"], "status": p["status"], "slot": p["slot"],
                                   "avail": round(S._avail(p), 3), "ret": p["ret"].isoformat() if p.get("ret") else None,
                                   "fp": round(S._fp(p["rates"]), 2) if p["rates"] else None,
                                   "vg": round(S._game_var(p["rates"]), 1) if p["rates"] else None,
                                   "pg": S._per_game(p["rates"]) if p["rates"] else None}
                                  for p in pl]})
    fixtures = [{"week": m["matchupPeriodId"], "home": m["home"]["teamId"], "away": m["away"]["teamId"]}
                for m in d["schedule"] if m["matchupPeriodId"] in weeks and m.get("away")]
    return {"weeks": [weeks[0], weeks[-1]], "teams": teams, "fixtures": fixtures,
            "scoring": {S.CN.get(k, k): v for k, v in S.SCORING.items()},
            "fa_fpg": fa_fpg,
            "consts": {"VAR_SCALE": S.VAR_SCALE, "FORM_SD": S.FORM_SD, "GAIN_CALIBRATION": S.GAIN_CALIBRATION,
                       "GAMES_PER_PLAYER": 3.4, "CAP": 38.0}}


def main():
    d = S._get(["mRoster", "mTeam", "mMatchup", "mMatchupScore", "mStatus", "mSettings", "mStandings"])
    names = {t["id"]: t.get("name").strip() for t in d["teams"]}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    o = odds(d, names); o["updated_utc"] = stamp
    m = model(d, names); m["updated_utc"] = stamp
    lg = league(d, names, LIVE); lg["updated_utc"] = stamp
    # keep ~30h of title/finals history so the page can show movement over the last day
    lpath = os.path.join(ROOT, "data", "league.json")
    try:
        hist = json.load(open(lpath)).get("history", [])
    except Exception:
        hist = []
    hist.append({"t": stamp, "title": {r["team"]: r["title_pct"] for r in lg["ladder"]},
                 "finals": {r["team"]: r["finals_pct"] for r in lg["ladder"]},
                 "power": {r["team"]: r["power_rank"] for r in lg["ladder"]}})
    hist = hist[-64:]
    now_ts = datetime.fromisoformat(stamp).timestamp()
    old = [h for h in hist if now_ts - datetime.fromisoformat(h["t"]).timestamp() >= 20 * 3600]
    ref = old[-1] if old else hist[0]
    for r in lg["ladder"]:
        r["title_move"] = round(r["title_pct"] - ref["title"].get(r["team"], r["title_pct"]), 1)
        r["finals_move"] = round(r["finals_pct"] - ref["finals"].get(r["team"], r["finals_pct"]), 1)
        r["power_move"] = (ref.get("power") or {}).get(r["team"], r["power_rank"]) - r["power_rank"]  # + = climbed
    lg["moves_since_utc"] = ref["t"]
    lg["history"] = hist
    for name, obj in (("league", lg), ("odds", o), ("model", m)):
        with open(os.path.join(ROOT, "data", f"{name}.json"), "w") as f:
            json.dump(obj, f, separators=(",", ":"))
    print(f"league: {len(lg['ladder'])} teams; odds: {len(o['matchups'])} matchups; model: {len(m['teams'])} teams, {len(m['fixtures'])} fixtures")


if __name__ == "__main__":
    main()
