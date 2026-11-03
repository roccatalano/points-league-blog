// Port of the points model in scripts/server.py (_weekly_dist, _cat_probs, _fixture_value) for the
// trade calculator. Each player carries fp (fantasy points per game) and vg (variance of one game).
(function (root) {
  function erf(x) { // Abramowitz-Stegun 7.1.26
    const s = x < 0 ? -1 : 1; x = Math.abs(x);
    const t = 1 / (1 + 0.3275911 * x);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
    return s * y;
  }
  function make(M) {
    const C = M.consts;
    const av = p => (typeof p.avail === "number" ? p.avail : 0.9);
    const fp = p => p.fp || 0;
    // weekly fantasy points of a roster: best players first, ~10 starters x 3.4 games of capacity
    function weeklyDist(players) {
      const ps = players.filter(p => p.fp != null && av(p) > 0).sort((a, b) => fp(b) - fp(a));
      let mean = 0, vr = 0, used = 0;
      for (const p of ps) {
        const g = Math.min(C.GAMES_PER_PLAYER * av(p), Math.max(0, C.CAP - used));
        if (g <= 0) break;
        used += g;
        mean += g * fp(p);
        vr += g * (p.vg || (C.FORM_SD * fp(p)) ** 2 + 2 * fp(p)) + 0.5 * fp(p) * fp(p);
      }
      return [mean, vr];
    }
    const winProb = (da, db) => {
      const sd = Math.sqrt(C.VAR_SCALE * (da[1] + db[1])) || 1;
      return 0.5 * (1 + erf((da[0] - db[0]) / sd / Math.SQRT2));
    };
    const rosterOf = id => M.teams.find(t => t.id === id).players;
    function fixtureValue(tid, rosters) {
      const mine = weeklyDist(rosters[tid] || rosterOf(tid));
      let exp = 0;
      for (const f of M.fixtures) {
        if (f.home !== tid && f.away !== tid) continue;
        const o = f.home === tid ? f.away : f.home;
        exp += winProb(mine, weeklyDist(rosters[o] || rosterOf(o)));
      }
      return exp;
    }
    const FA = { name: "Free agent", status: "ACTIVE", slot: 0, avail: 0.93, fp: M.fa_fpg || 20, vg: null };
    const active = r => r.filter(p => p.slot !== 13).length;  // IR spots don't count toward the roster limit
    // Uneven trades: the team that ends up with too many players drops its least valuable one;
    // the team left with an open spot picks up an average free agent.
    function balance(before, after, protect) {
      const r = after.slice(), moves = { drops: [], adds: 0 };
      while (active(r) > active(before)) {
        const cands = r.filter(p => p.slot !== 13 && !protect.includes(p));
        const pool = cands.length ? cands : r.filter(p => p.slot !== 13);
        const worst = pool.reduce((x, y) => (av(y) * fp(y) < av(x) * fp(x) ? y : x));
        r.splice(r.indexOf(worst), 1); moves.drops.push(worst.name);
      }
      while (active(r) < active(before)) { r.push(Object.assign({}, FA)); moves.adds++; }
      return [r, moves];
    }
    // give = names leaving team a, get = names leaving team b
    function evaluate(a, b, giveNames, getNames) {
      const ra = rosterOf(a), rb = rosterOf(b);
      const give = ra.filter(p => giveNames.includes(p.name)), get = rb.filter(p => getNames.includes(p.name));
      let [na, ma] = balance(ra, ra.filter(p => !give.includes(p)).concat(get), get);
      let [nb, mb] = balance(rb, rb.filter(p => !get.includes(p)).concat(give), give);
      const after = { [a]: na, [b]: nb };
      const side = (tid, before, aft) => {
        const bv = fixtureValue(tid, { [tid]: before });
        const avv = fixtureValue(tid, Object.assign({}, after, { [tid]: aft }));
        return { change: (avv - bv) * C.GAIN_CALIBRATION,
                 weekly: weeklyDist(aft)[0] - weeklyDist(before)[0],   // fantasy points per week
                 per_game_in: get.concat(give).length ? null : null };
      };
      return { a: Object.assign(side(a, ra, na), { moves: ma }), b: Object.assign(side(b, rb, nb), { moves: mb }) };
    }
    return { evaluate, weeklyDist, winProb, fixtureValue };
  }
  root.LeagueModel = { make };
  if (typeof module !== "undefined") module.exports = { make };
})(typeof window !== "undefined" ? window : globalThis);
