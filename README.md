# NBA Fantasy 2026

League blog for an ESPN **head-to-head points** league (league 2039896436, public): live odds,
ladder, trade calculator, injuries, trade tracker, model record, and daily / weekly / trade posts.

- `scripts/server.py` – the points model. Reads the league's points table from ESPN, simulates each week
  day by day (real NBA schedule, best lineup each day, injuries until their return date) and gives every
  matchup a win chance, projected final score and 80% margin range. Ported from the 9-category model.
- `.github/workflows/league-odds.yml` – every 30 minutes: odds, ladder, calculator data, margin history,
  trade tracker, model record, injuries, and a trade post when a new trade appears.
- `.github/workflows/league-blog.yml` – daily post 6:45pm and weekly roundup Monday 7:15pm (Melbourne,
  daylight saving time). Posts are written by Claude from `data/latest.json` following `WRITING.md`.
- Needs one repository secret: `CLAUDE_CODE_OAUTH_TOKEN`. No ESPN cookies (the league is public).
