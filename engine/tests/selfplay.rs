//! Plays games of random orders, valid and invalid, checking that the engine
//! never panics, keeps its bookkeeping consistent, and replays identically.

use bitbath_engine::mapgen::generate;
use bitbath_engine::rng::SplitMix64;
use bitbath_engine::{Game, Rules};

fn random_order(g: &Game, rng: &mut SplitMix64) -> String {
    let mine: Vec<u32> = g.units.iter().filter(|u| u.owner == g.player).map(|u| u.id).collect();
    let any_unit = |rng: &mut SplitMix64| -> u32 {
        if !mine.is_empty() && rng.below(5) > 0 {
            mine[rng.below(mine.len() as u64) as usize]
        } else {
            rng.below(g.next_unit_id as u64 + 2) as u32
        }
    };
    match rng.below(20) {
        0..=9 => {
            let len = 1 + rng.below(5);
            let dirs: String = (0..len).map(|_| char::from(b'0' + rng.below(6) as u8)).collect();
            format!("M {} {dirs}", any_unit(rng))
        }
        10..=13 => {
            let target = g.units[rng.below(g.units.len() as u64) as usize].id;
            format!("A {} {target}", any_unit(rng))
        }
        14..=15 => format!("C {}", any_unit(rng)),
        16..=18 => {
            let codes = ['I', 'P', 'K', 'A', 'S', 'Z'];
            format!(
                "B {} {}",
                rng.below(g.cities.len() as u64 + 1),
                codes[rng.below(6) as usize]
            )
        }
        _ => ["", "M", "M 1 7", "A x y", "B 0", "C 1 2", "Q", "M 99999999999 0"][rng.below(8) as usize].to_string(),
    }
}

fn snapshot(g: &Game) -> String {
    format!("{:?} {:?} {:?} {} {:?}", g.units, g.cities, g.gold, g.round, g.outcome)
}

#[test]
fn random_orders_keep_the_game_consistent() {
    let mut rules = Rules::default();
    rules.c.map_width = 16;
    rules.c.map_height = 12;
    rules.c.neutral_cities = 4;
    rules.c.round_limit = 60;
    let mut finished = 0;
    for seed in 0..8u64 {
        let map = generate(&rules, seed).unwrap();
        let mut g = Game::new(rules.clone(), map.clone());
        let mut rng = SplitMix64::new(seed);
        let mut script: Vec<(u8, Vec<String>)> = Vec::new();
        'game: for _ in 0..rules.c.round_limit {
            for player in [1u8, 2] {
                g.begin_turn(player);
                let mut orders = Vec::new();
                for _ in 0..30 {
                    let order = random_order(&g, &mut rng);
                    let reply = g.apply(&order);
                    let last = reply.last().expect("every order gets a reply");
                    assert!(
                        last.starts_with("OK") || last.starts_with("PART") || last.starts_with("ERR"),
                        "'{order}' -> {reply:?}"
                    );
                    assert!(reply[..reply.len() - 1].iter().all(|l| l.starts_with("SEE ")));
                    g.check_invariants()
                        .unwrap_or_else(|e| panic!("seed {seed}, after '{order}': {e}"));
                    orders.push(order);
                    if g.outcome.is_some() {
                        break;
                    }
                }
                script.push((player, orders));
                g.end_turn();
                if g.outcome.is_some() {
                    break 'game;
                }
            }
        }
        if g.outcome.is_some() {
            finished += 1;
        }

        // Re-simulating the same orders must give exactly the same game.
        let mut again = Game::new(rules.clone(), map);
        for (player, orders) in &script {
            again.begin_turn(*player);
            for order in orders {
                again.apply(order);
            }
            again.end_turn();
        }
        assert_eq!(snapshot(&g), snapshot(&again), "seed {seed}");
    }
    assert!(
        finished > 0,
        "no game finished, so the round limit or elimination never triggered"
    );
}
