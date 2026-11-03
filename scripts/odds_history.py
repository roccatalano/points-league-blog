"""Projected-margin history for every matchup, for the live charts on the odds page.
Appends the current data/odds.json to data/odds_history.json. Run after live.py.

Format: {"<period>": {"<home>|<away>": [[iso_utc, proj_margin, p10, p90, home_win_pct, pts_home, pts_away], ...]}}
(margin = home minus away projected final points; p10/p90 = 80% range)
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PATH = os.path.join(ROOT, "data", "odds_history.json")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def add(hist, o):
    if not o or not o.get("matchups") or not o.get("updated_utc"):
        return False
    wk = hist.setdefault(str(o["matchup_period"]), {})
    ts = o["updated_utc"][:19] + "Z"
    new = False
    for m in o["matchups"]:
        pts = wk.setdefault(f'{m["home"]}|{m["away"]}', [])
        if any(p[0] == ts for p in pts):
            continue
        lo, hi = m["margin_80"]
        pts.append([ts, m["proj_margin"], lo, hi, m["home_win_pct"], m["points_now"][0], m["points_now"][1]])
        pts.sort(key=lambda p: p[0])
        new = True
    return new


def main():
    hist = load(PATH, {})
    if add(hist, load(os.path.join(ROOT, "data", "odds.json"), None)):
        with open(PATH, "w") as f:
            json.dump(hist, f, separators=(",", ":"))
        print("odds history updated")
    else:
        print("odds history unchanged")


if __name__ == "__main__":
    main()
