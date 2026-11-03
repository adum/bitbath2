//! Seeded, point-symmetric map generation (SPEC §3.9). Integer math only.

use crate::map::{CityInfo, Map};
use crate::rng::{mix64, SplitMix64};
use crate::rules::{Rules, CITY_CHAR};

/// Bumped whenever generated maps change for the same seed and rules.
pub const GENERATOR_VERSION: u32 = 1;

/// Generates the map for `seed` using `rules.c` for its size and number of
/// neutral cities. Rerolls deterministically until the map is valid.
pub fn generate(rules: &Rules, seed: u64) -> Result<Map, String> {
    let w = rules.c.map_width as i32;
    let h = rules.c.map_height as i32;
    if w < 8 || h < 6 || h % 2 != 0 {
        return Err("generated maps need map_width >= 8 and an even map_height >= 6".into());
    }
    if !rules.c.neutral_cities.is_multiple_of(2) {
        return Err("neutral_cities must be even, since generated cities come in mirrored pairs".into());
    }
    let terrain = |ch: char| {
        rules
            .terrain_index(ch)
            .map(|t| t as u8)
            .ok_or_else(|| format!("the generator needs terrain '{ch}'"))
    };
    let t = Palette {
        plains: terrain('.')?,
        forest: terrain('f')?,
        hills: terrain('h')?,
        city: terrain(CITY_CHAR)?,
        mountains: terrain('^')?,
        water: terrain('~')?,
    };
    for attempt in 0..1000u64 {
        let mut rng = SplitMix64::new(mix64(seed ^ mix64(attempt.wrapping_add(1))));
        if let Some(map) = try_generate(rules, &t, w, h, &mut rng) {
            return Ok(map);
        }
    }
    Err(format!("couldn't generate a valid {w}x{h} map for seed {seed}"))
}

struct Palette {
    plains: u8,
    forest: u8,
    hills: u8,
    city: u8,
    mountains: u8,
    water: u8,
}

/// Smoothstep weight for offset `t` within a lattice cell of size `s`, in `0..=s`.
fn smooth(t: u32, s: u32) -> u32 {
    t * t * (3 * s - 2 * t) / (s * s)
}

fn lattice(seed: u64, gx: u32, gy: u32) -> u32 {
    (mix64(seed ^ ((gx as u64) << 32) ^ gy as u64) % 1024) as u32
}

/// Value noise in `0..1024` at point `(x, y)` with lattice spacing `s`.
fn noise(seed: u64, x: u32, y: u32, s: u32) -> u32 {
    let (gx, gy) = (x / s, y / s);
    let fx = smooth(x % s, s);
    let fy = smooth(y % s, s);
    let top = lattice(seed, gx, gy) * (s - fx) + lattice(seed, gx + 1, gy) * fx;
    let bottom = lattice(seed, gx, gy + 1) * (s - fx) + lattice(seed, gx + 1, gy + 1) * fx;
    (top * (s - fy) + bottom * fy) / (s * s)
}

/// The value at the given per-mille rank of `values`.
fn percentile(values: &[u32], per_mille: usize) -> u32 {
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    sorted[(sorted.len() * per_mille / 1000).min(sorted.len() - 1)]
}

fn try_generate(rules: &Rules, t: &Palette, w: i32, h: i32, rng: &mut SplitMix64) -> Option<Map> {
    let n = (w * h) as usize;
    let (e1, e2, m1) = (rng.next_u64(), rng.next_u64(), rng.next_u64());

    // Noise sampled at hex centers: 8 units per hex across, about 7 per row.
    let mut elevation = vec![0u32; n];
    let mut moisture = vec![0u32; n];
    for k in 0..n {
        let (col, row) = ((k as i32 % w) as u32, (k as i32 / w) as u32);
        let (x, y) = (8 * col + 4 * (row & 1), 7 * row);
        elevation[k] = (2 * noise(e1, x, y, 44) + noise(e2, x, y, 20)) / 3;
        moisture[k] = noise(m1, x, y, 32);
    }
    // Averaging each cell with its mirror makes the fields point-symmetric
    // without a seam across the middle.
    for k in 0..n / 2 {
        let m = n - 1 - k;
        let e = (elevation[k] + elevation[m]) / 2;
        elevation[k] = e;
        elevation[m] = e;
        let wet = (moisture[k] + moisture[m]) / 2;
        moisture[k] = wet;
        moisture[m] = wet;
    }

    let water_below = percentile(&elevation, 60);
    let hills_from = percentile(&elevation, 830);
    let mountains_from = percentile(&elevation, 935);
    let forest_from = percentile(&moisture, 760);
    let mut tiles: Vec<u8> = (0..n)
        .map(|k| {
            let e = elevation[k];
            if e < water_below {
                t.water
            } else if e >= mountains_from {
                t.mountains
            } else if e >= hills_from {
                t.hills
            } else if moisture[k] >= forest_from {
                t.forest
            } else {
                t.plains
            }
        })
        .collect();

    let mut map = Map {
        width: w,
        height: h,
        tiles: Vec::new(),
        cities: Vec::new(),
    };
    let passable = |tile: u8| rules.terrains[tile as usize].passable();

    // Home cities: player 1 near the top, player 2 at the mirror image.
    let home_row = rng.range(1, (h / 6).max(1));
    let home_col = rng.range(2, w - 3);
    let home = (home_row * w + home_col) as usize;
    let mut sites: Vec<usize> = vec![home, n - 1 - home];

    // Neutral cities in mirrored pairs, placed in the top half.
    let min_gap = ((w + h) / 14).max(3) as u32;
    let far_enough = |map: &Map, sites: &[usize], k: usize| {
        let hx = map.hex(k);
        let mirror = map.hex(n - 1 - k);
        hx.distance(mirror) >= min_gap
            && sites
                .iter()
                .all(|&s| map.hex(s).distance(hx) >= min_gap && map.hex(s).distance(mirror) >= min_gap)
    };
    let pairs = (rules.c.neutral_cities / 2) as usize;
    let mut tries = 0;
    while sites.len() < 2 + 2 * pairs {
        tries += 1;
        if tries > 4000 {
            return None;
        }
        let k = rng.below((n / 2) as u64) as usize;
        if passable(tiles[k]) && far_enough(&map, &sites, k) {
            sites.push(k);
            sites.push(n - 1 - k);
        }
    }

    // Homes never start boxed in by impassable terrain.
    for &s in &sites[..2] {
        for nb in map.hex(s).neighbors() {
            if map.contains(nb) {
                let i = map.index(nb);
                if !passable(tiles[i]) {
                    tiles[i] = t.plains;
                }
            }
        }
    }
    for &s in &sites {
        tiles[s] = t.city;
    }
    map.tiles = tiles;

    // Every city must be reachable from player 1's home. By symmetry it is
    // then reachable from player 2's home too.
    let mut seen = vec![false; n];
    let mut queue = vec![sites[0]];
    seen[sites[0]] = true;
    while let Some(k) = queue.pop() {
        for nb in map.hex(k).neighbors() {
            if map.contains(nb) {
                let i = map.index(nb);
                if !seen[i] && passable(map.tiles[i]) {
                    seen[i] = true;
                    queue.push(i);
                }
            }
        }
    }
    if sites.iter().any(|&s| !seen[s]) {
        return None;
    }

    let owners = [1u8, 2u8];
    map.cities = sites
        .iter()
        .enumerate()
        .map(|(i, &s)| CityInfo {
            hex: map.hex(s),
            owner: if i < 2 { owners[i] } else { 0 },
        })
        .collect();
    Some(map)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_and_symmetric() {
        let rules = Rules::default();
        for seed in [1u64, 2, 42, 12345] {
            let a = generate(&rules, seed).unwrap();
            let b = generate(&rules, seed).unwrap();
            assert_eq!(a, b);
            a.validate(&rules).unwrap();
            let n = a.len();
            for k in 0..n {
                assert_eq!(a.tiles[k], a.tiles[n - 1 - k], "seed {seed} tile {k}");
            }
            assert_eq!(a.cities.len(), 12);
            assert_eq!(a.cities[0].owner, 1);
            assert_eq!(a.cities[1].owner, 2);
            for pair in a.cities.chunks(2) {
                assert_eq!(a.mirror(pair[0].hex), pair[1].hex);
            }
        }
        assert_ne!(generate(&rules, 1).unwrap(), generate(&rules, 2).unwrap());
    }

    #[test]
    fn small_maps() {
        let mut rules = Rules::default();
        rules.c.map_width = 16;
        rules.c.map_height = 12;
        rules.c.neutral_cities = 4;
        for seed in 0..20 {
            let m = generate(&rules, seed).unwrap();
            m.validate(&rules).unwrap();
        }
    }
}
