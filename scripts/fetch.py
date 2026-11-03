"""Pull the data for a blog post. Writes data/latest.json for Claude to write from (see WRITING.md).

Usage: python scripts/fetch.py daily|weekly|trade
Trade reviews come from data/trades.json (scripts/trades.py runs first); each trade is reviewed once.
"""
import json, os, sys, logging
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

REVIEWED = os.path.join(HERE, "blog_trades_reviewed.json")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def trade_reviews(mark=True):
    """Trades in data/trades.json that haven't had a post yet, with the model's view and each
    player's per-game numbers."""
    seen = set(_load(REVIEWED, []))
    tr = _load(os.path.join(ROOT, "data", "trades.json"), {"trades": []})
    model = _load(os.path.join(ROOT, "data", "model.json"), {"teams": []})
    by_name = {p["name"]: p for t in model["teams"] for p in t["players"]}
    out = []
    for t in tr["trades"]:
        if t["key"] in seen:
            continue
        def brief(n):
            p = by_name.get(n) or {}
            return {"name": n, "fpts_per_game": p.get("fp"), "per_game": p.get("pg"), "injury": p.get("status")}
        A, B = t["teams"]
        out.append({"processed_utc": t["processed_utc"], "status": "processed",
                    "sides": [{"team": s, "gets": [brief(n) for n in t["gets"][s]],
                               "gives": [brief(n) for n in t["gets"][o]],
                               "model_rest_of_season": (t.get("now") or t.get("at_time") or {}).get(s)}
                              for s, o in ((A, B), (B, A))],
                    "so_far": {k: t["so_far"][k] for k in ("days", "points", "margin", "players", "fa_credit")},
                    "avg_free_agent_fpts_per_game": tr.get("fa_points_per_game")})
        seen.add(t["key"])
    if mark:
        with open(REVIEWED, "w") as f:
            json.dump(sorted(seen), f, indent=1)
    return out


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "daily"
    now_mel = datetime.now(S.MEL)
    data = {"kind": kind, "generated_utc": datetime.now(timezone.utc).isoformat(),
            "date_melbourne_approx": now_mel.strftime("%Y-%m-%d"),
            "league": {"format": "head-to-head points", "scoring": {S.CN.get(k, k): v for k, v in S.SCORING.items()}}}
    out_path = os.path.join(ROOT, "data", "latest.json")
    if kind == "weekly":
        data["weekly"] = json.loads(S.weekly_post_data())
    elif kind == "trade":
        data["trades"] = trade_reviews()
        if not data["trades"]:
            if os.path.exists(out_path):
                os.remove(out_path)
            print("no new trades")
            return
    else:
        data["daily"] = json.loads(S.daily_post_data())
        data["trades"] = trade_reviews()
        inj = _load(os.path.join(ROOT, "data", "injuries.json"), {})
        data["injury_report"] = [
            {k: x.get(k) for k in ("name", "nba_team", "fantasy_team", "on_ir", "status", "injury",
                                   "return_date", "games_missed", "timeframe", "note", "per_game")}
            for x in inj.get("players", []) if x.get("fantasy_team")]
    with open(out_path, "w") as f:
        json.dump(data, f, indent=1, default=str)
    print(f"wrote data/latest.json ({kind}), trades: {len(data.get('trades', []))}")


if __name__ == "__main__":
    main()
