# NBA Fantasy 2026

League blog for an ESPN **head-to-head points** league: live win odds, daily and weekly posts, trade reviews.

- `scripts/server.py` – the points model. Reads the league's points table from ESPN, simulates each week day by day
  (real NBA schedule, daily lineups, injuries) and gives every matchup a win chance.
- Ported from the 9-category model in `league-blog`.
