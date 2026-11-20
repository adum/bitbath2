//! Ratings from tournament games: a Bradley–Terry fit on the Elo scale, with
//! bootstrap intervals (SPEC §11).

use crate::tournament::Record;
use bitbath_engine::rng::SplitMix64;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fmt::Write;

/// The rating that the bots average when no bot is anchored.
const MEAN_RATING: f64 = 1500.0;
/// Reasons for a result that mean the loser's program failed.
const FORFEITS: [&str; 4] = ["crash", "memory", "hang", "init"];

pub struct Options {
    /// A bot whose rating is fixed, which sets everyone else's. Without one,
    /// the ratings average 1500.
    pub anchor: Option<(String, f64)>,
    /// Resamples for the intervals, or 0 for none.
    pub bootstrap: usize,
}

/// One bot's rating, record, and diagnostics.
pub struct Row {
    pub name: String,
    pub rating: f64,
    /// The 95% interval, when there is one.
    pub interval: Option<(f64, f64)>,
    pub games: u64,
    pub wins: u64,
    pub draws: u64,
    pub losses: u64,
    /// Games lost because the bot crashed, hung, ran out of memory, or
    /// didn't start.
    pub forfeits: u64,
    pub orders: u64,
    pub invalid: u64,
    pub turns: u64,
    pub think_ms: u64,
    pub max_think_ms: u64,
}

/// A game between bots `a` and `b`, with what `a` scored: 1, 0.5, or 0.
type Score = (usize, usize, f64);

/// Rates every bot that appears in `games`. The rows come back best first.
pub fn rate(games: &[Record], opts: &Options) -> Result<Vec<Row>, String> {
    let mut index = BTreeMap::new();
    for g in games {
        for name in &g.players {
            let next = index.len();
            index.entry(name.clone()).or_insert(next);
        }
    }
    let anchor = match &opts.anchor {
        Some((name, rating)) => Some((
            *index
                .get(name)
                .ok_or_else(|| format!("the anchor '{name}' played no games"))?,
            *rating,
        )),
        None => None,
    };
    let mut rows: Vec<Row> = (0..index.len()).map(|_| Row::new()).collect();
    for (name, &i) in &index {
        rows[i].name = name.clone();
    }

    // The two games of a pair on one map are resampled together: they share
    // the map, so they aren't independent.
    let mut blocks: BTreeMap<(String, u64, usize, usize), Vec<Score>> = BTreeMap::new();
    for g in games {
        let (a, b) = (index[&g.players[0]], index[&g.players[1]]);
        let score = match g.winner {
            1 => 1.0,
            2 => 0.0,
            _ => 0.5,
        };
        blocks
            .entry((g.config.clone(), g.seed, a.min(b), a.max(b)))
            .or_default()
            .push((a, b, score));
        let forfeit = FORFEITS.contains(&g.reason.as_str());
        for (seat, &i) in [a, b].iter().enumerate() {
            let row = &mut rows[i];
            row.games += 1;
            if g.winner == 0 {
                row.draws += 1;
                row.forfeits += forfeit as u64;
            } else if g.winner as usize == seat + 1 {
                row.wins += 1;
            } else {
                row.losses += 1;
                row.forfeits += forfeit as u64;
            }
            let stat = |key: &str| g.stats[seat][key].as_u64().unwrap_or(0);
            row.orders += stat("orders");
            row.invalid += stat("invalid");
            row.turns += stat("turns");
            row.think_ms += stat("think_ms");
            row.max_think_ms = row.max_think_ms.max(stat("max_think_ms"));
        }
    }
    let blocks: Vec<Vec<Score>> = blocks.into_values().collect();

    let n = rows.len();
    let ratings = fit(n, blocks.iter().flatten(), anchor);
    let mut samples = vec![Vec::with_capacity(opts.bootstrap); n];
    if !blocks.is_empty() {
        // A fixed seed: the same games always give the same intervals.
        let mut rng = SplitMix64::new(0x0B17_BA7B);
        for _ in 0..opts.bootstrap {
            let draw: Vec<&Vec<Score>> = (0..blocks.len())
                .map(|_| &blocks[rng.below(blocks.len() as u64) as usize])
                .collect();
            for (i, r) in fit(n, draw.into_iter().flatten(), anchor).into_iter().enumerate() {
                samples[i].push(r);
            }
        }
    }
    for (i, row) in rows.iter_mut().enumerate() {
        row.rating = ratings[i];
        let s = &mut samples[i];
        if !s.is_empty() {
            s.sort_by(f64::total_cmp);
            let at = |q: f64| s[((s.len() - 1) as f64 * q).round() as usize];
            row.interval = Some((at(0.025), at(0.975)));
        }
    }
    rows.sort_by(|a, b| b.rating.total_cmp(&a.rating).then_with(|| a.name.cmp(&b.name)));
    Ok(rows)
}

/// Fits Bradley–Terry strengths to the games and returns them as ratings,
/// where 400 points mean 10-to-1 odds. A draw counts as half a win for each
/// side.
///
/// Every bot also gets one drawn game against an imaginary average bot.
/// Without it, a bot that won or lost all its games would have no finite
/// rating.
fn fit<'a>(n: usize, games: impl Iterator<Item = &'a Score>, anchor: Option<(usize, f64)>) -> Vec<f64> {
    let mut won = vec![0.5; n];
    let mut met = vec![vec![0.0; n]; n];
    for &(a, b, score) in games {
        won[a] += score;
        won[b] += 1.0 - score;
        met[a][b] += 1.0;
        met[b][a] += 1.0;
    }
    // Minorize-maximize steps: each raises the likelihood, and they converge
    // to its one maximum.
    let mut p = vec![1.0f64; n];
    for _ in 0..10_000 {
        let mut change = 0.0f64;
        for i in 0..n {
            let mut den = 1.0 / (p[i] + 1.0);
            for j in 0..n {
                if met[i][j] > 0.0 {
                    den += met[i][j] / (p[i] + p[j]);
                }
            }
            let next = won[i] / den;
            change = change.max((next / p[i]).ln().abs());
            p[i] = next;
        }
        if change < 1e-9 {
            break;
        }
    }
    let mut ratings: Vec<f64> = p.iter().map(|s| 400.0 * s.log10()).collect();
    let shift = match anchor {
        Some((i, rating)) => rating - ratings[i],
        None if n > 0 => MEAN_RATING - ratings.iter().sum::<f64>() / n as f64,
        None => 0.0,
    };
    for r in ratings.iter_mut() {
        *r += shift;
    }
    ratings
}

impl Row {
    fn new() -> Row {
        Row {
            name: String::new(),
            rating: 0.0,
            interval: None,
            games: 0,
            wins: 0,
            draws: 0,
            losses: 0,
            forfeits: 0,
            orders: 0,
            invalid: 0,
            turns: 0,
            think_ms: 0,
            max_think_ms: 0,
        }
    }

    /// The share of the bot's orders that the engine rejected, in percent.
    fn invalid_pct(&self) -> f64 {
        100.0 * self.invalid as f64 / self.orders.max(1) as f64
    }

    fn avg_think_ms(&self) -> f64 {
        self.think_ms as f64 / self.turns.max(1) as f64
    }
}

/// The ratings as a text table.
pub fn table(rows: &[Row]) -> String {
    let width = rows.iter().map(|r| r.name.len()).max().unwrap_or(0).max(3);
    let mut out = format!(
        "  #  {:<width$}  rating  95% interval  games     W     D     L  forfeits  invalid  avg ms  max ms\n",
        "bot"
    );
    for (i, r) in rows.iter().enumerate() {
        let interval = match r.interval {
            Some((lo, hi)) => format!("{lo:.0}..{hi:.0}"),
            None => "-".to_string(),
        };
        writeln!(
            out,
            "{:>3}  {:<width$}  {:>6.0}  {interval:>12}  {:>5} {:>5} {:>5} {:>5}  {:>8}  {:>6.1}%  {:>6.0}  {:>6}",
            i + 1,
            r.name,
            r.rating,
            r.games,
            r.wins,
            r.draws,
            r.losses,
            r.forfeits,
            r.invalid_pct(),
            r.avg_think_ms(),
            r.max_think_ms
        )
        .expect("writing to a string");
    }
    out
}

/// The ratings as JSON, for `ratings.json`.
pub fn to_json(rows: &[Row], games: usize, opts: &Options) -> Value {
    let round = |x: f64| (x * 10.0).round() / 10.0;
    json!({
        "games": games,
        "anchor": opts.anchor.as_ref().map(|(name, rating)| json!({"bot": name, "rating": rating})),
        "bootstrap": opts.bootstrap,
        "ratings": rows.iter().map(|r| json!({
            "bot": r.name,
            "rating": round(r.rating),
            "interval": r.interval.map(|(lo, hi)| [round(lo), round(hi)]),
            "games": r.games,
            "wins": r.wins,
            "draws": r.draws,
            "losses": r.losses,
            "forfeits": r.forfeits,
            "invalid_pct": (r.invalid_pct() * 100.0).round() / 100.0,
            "avg_think_ms": round(r.avg_think_ms()),
            "max_think_ms": r.max_think_ms,
        })).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `count` games on different maps, with the given winner each time.
    fn games(p1: &str, p2: &str, winner: u8, reason: &str, count: u64, out: &mut Vec<Record>) {
        for _ in 0..count {
            let line = json!({"config": "micro", "seed": out.len(), "p1": p1, "p2": p2, "winner": winner,
                "reason": reason, "stats": [{"orders": 10, "invalid": 1, "turns": 5, "think_ms": 50, "max_think_ms": 20}, {}]});
            out.push(Record::from_json(line).unwrap());
        }
    }

    fn options(bootstrap: usize) -> Options {
        Options {
            anchor: None,
            bootstrap,
        }
    }

    #[test]
    fn stronger_bots_rate_higher() {
        let mut g = Vec::new();
        games("a", "b", 1, "cities", 6, &mut g);
        games("b", "a", 1, "cities", 2, &mut g);
        games("b", "c", 1, "crash", 8, &mut g);
        games("c", "a", 0, "draw", 2, &mut g);
        let rows = rate(&g, &options(200)).unwrap();
        let names: Vec<&str> = rows.iter().map(|r| r.name.as_str()).collect();
        assert_eq!(names, ["a", "b", "c"]);
        let mean = rows.iter().map(|r| r.rating).sum::<f64>() / 3.0;
        assert!((mean - MEAN_RATING).abs() < 1e-6, "{mean}");
        for r in &rows {
            let (lo, hi) = r.interval.unwrap();
            assert!(lo <= r.rating && r.rating <= hi, "{}: {lo} {} {hi}", r.name, r.rating);
        }
        let c = &rows[2];
        assert_eq!((c.games, c.wins, c.draws, c.losses, c.forfeits), (10, 0, 2, 8, 8));
        let a = &rows[0];
        assert_eq!((a.orders, a.invalid, a.max_think_ms), (60, 6, 20));
    }

    /// Two bots that win three games in four are about 191 points apart, less
    /// the pull of each one's drawn game against the average bot.
    #[test]
    fn the_scale_is_elo() {
        let mut g = Vec::new();
        games("a", "b", 1, "cities", 300, &mut g);
        games("a", "b", 2, "cities", 100, &mut g);
        let rows = rate(&g, &options(0)).unwrap();
        let gap = rows[0].rating - rows[1].rating;
        assert!((gap - 190.8).abs() < 2.0, "{gap}");
        assert!(rows[0].interval.is_none());
    }

    #[test]
    fn an_unbeaten_bot_has_a_finite_rating() {
        let mut g = Vec::new();
        games("a", "b", 1, "cities", 10, &mut g);
        let rows = rate(&g, &options(50)).unwrap();
        assert!(rows[0].rating.is_finite() && rows[0].rating > rows[1].rating);
        assert!(rows[0].interval.unwrap().1.is_finite());
    }

    #[test]
    fn an_anchor_fixes_its_own_rating() {
        let mut g = Vec::new();
        games("a", "b", 1, "cities", 5, &mut g);
        games("a", "b", 2, "cities", 3, &mut g);
        let mut opts = options(50);
        opts.anchor = Some(("b".into(), 1000.0));
        let rows = rate(&g, &opts).unwrap();
        let b = rows.iter().find(|r| r.name == "b").unwrap();
        assert!((b.rating - 1000.0).abs() < 1e-6);
        assert_eq!(
            b.interval.map(|(lo, hi)| (lo.round(), hi.round())),
            Some((1000.0, 1000.0))
        );
        opts.anchor = Some(("nobody".into(), 1000.0));
        assert!(rate(&g, &opts).is_err());
    }
}
