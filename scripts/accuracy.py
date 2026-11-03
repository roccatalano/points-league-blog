"""Model track record for a POINTS league. Writes data/prediction_log.json and data/accuracy.json.

- Forecasts: the first live odds of each week (data/odds.json), saved once per week. It only counts as an
  "opening" forecast if no points had been scored yet (Monday before tip-off); later first-sightings are
  kept but marked and left out of the scores.
- Results: final fantasy points from ESPN once every matchup in the week has a winner.
- Scores: winners called, Brier score (0.25 = coin flip), calibration buckets, projected vs actual team
  points (average miss and bias), and how often the final margin landed inside the 80% range.
Run after live.py.
"""
import json, os, sys, logging
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

LOG = os.path.join(ROOT, "data", "prediction_log.json")
OUT = os.path.join(ROOT, "data", "accuracy.json")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_forecast(log, o):
    if not o or not o.get("matchups"):
        return
    wk = str(o["matchup_period"])
    if wk in log["forecasts"]:
        return
    started = any(sum(m["points_now"]) > 0 for m in o["matchups"])
    log["forecasts"][wk] = {"saved_utc": o["updated_utc"], "opening": not started,
                            "matchups": {f'{m["home_id"]}-{m["away_id"]}': {
                                "home": m["home"], "away": m["away"], "p_home": m["home_win_pct"] / 100,
                                "p_tie": m.get("tie_pct", 0) / 100, "proj": m["proj_final"],
                                "margin_80": m["margin_80"]} for m in o["matchups"]}}


def save_results(log, d):
    by = {}
    for m in d["schedule"]:
        if "away" in m:
            by.setdefault(m["matchupPeriodId"], []).append(m)
    for p, ms in by.items():
        if str(p) in log["results"] or str(p) not in log["forecasts"]:
            continue
        if any(m.get("winner") in (None, "UNDECIDED") for m in ms):
            continue
        log["results"][str(p)] = {f'{m["home"]["teamId"]}-{m["away"]["teamId"]}': {
            "home_pts": S._side_points(m["home"], live=False), "away_pts": S._side_points(m["away"], live=False),
            "winner": m["winner"]} for m in ms}


def score(log):
    rows, weeks = [], []
    for wk, f in sorted(log["forecasts"].items(), key=lambda x: int(x[0])):
        res = log["results"].get(wk)
        wrow = {"week": int(wk), "opening": f["opening"], "saved_utc": f["saved_utc"], "finished": bool(res), "matchups": []}
        for key, fc in f["matchups"].items():
            r = (res or {}).get(key)
            m = {"home": fc["home"], "away": fc["away"], "home_win_pct": round(fc["p_home"] * 100),
                 "proj": fc["proj"], "margin_80": fc["margin_80"]}
            if r:
                won = 1.0 if r["winner"] == "HOME" else 0.5 if r["winner"] == "TIE" else 0.0
                margin = r["home_pts"] - r["away_pts"]
                p = fc["p_home"] + fc["p_tie"] / 2
                m.update({"actual": [r["home_pts"], r["away_pts"]],
                          "called": (p > 0.5) == (won == 1.0) if won != 0.5 and p != 0.5 else None,
                          "brier": round((p - won) ** 2, 3),
                          "in_range": fc["margin_80"][0] <= margin <= fc["margin_80"][1],
                          "miss": [round(r["home_pts"] - fc["proj"][0], 1), round(r["away_pts"] - fc["proj"][1], 1)]})
                if f["opening"]:
                    rows.append((p, won, m))
            wrow["matchups"].append(m)
        weeks.append(wrow)
    summ = None
    if rows:
        misses = [x for _, _, m in rows for x in m["miss"]]
        called = [m["called"] for _, _, m in rows if m["called"] is not None]
        buckets = {}
        for p, won, _ in rows:
            fav = max(p, 1 - p)
            b = min(4, int((fav - 0.5) * 10))  # 50-60, 60-70, 70-80, 80-90, 90-100
            lab = f"{50 + 10 * b}-{60 + 10 * b}%"
            buckets.setdefault(lab, []).append(won if p >= 0.5 else 1 - won)
        summ = {"matchups": len(rows), "called": sum(called), "callable": len(called),
                "brier": round(sum(m["brier"] for _, _, m in rows) / len(rows), 3),
                "avg_miss_points": round(sum(abs(x) for x in misses) / len(misses), 1),
                "bias_points": round(sum(misses) / len(misses), 1),  # + = teams scored more than projected
                "in_range": sum(m["in_range"] for _, _, m in rows), "range_target_pct": 80,
                "calibration": [{"bucket": k, "n": len(v), "fav_won_pct": round(100 * sum(v) / len(v))}
                                for k, v in sorted(buckets.items())]}
    return weeks, summ


def main():
    log = load(LOG, {"forecasts": {}, "results": {}})
    save_forecast(log, load(os.path.join(ROOT, "data", "odds.json"), None))
    d = S._get(["mMatchup", "mMatchupScore", "mStatus"])
    save_results(log, d)
    weeks, summ = score(log)
    with open(LOG, "w") as f:
        json.dump(log, f, separators=(",", ":"))
    with open(OUT, "w") as f:
        json.dump({"updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "summary": summ, "weeks": weeks}, f, indent=1)
    print(f"accuracy: {len(weeks)} weeks logged, {summ['matchups'] if summ else 0} scored")


if __name__ == "__main__":
    main()
