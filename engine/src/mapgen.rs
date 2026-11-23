//! Seeded, point-symmetric map generation (SPEC §3.9). Integer math only.

use crate::map::{CityInfo, Map, SiteInfo};
use crate::rng::{mix64, SplitMix64};
use crate::rules::{Rules, CITY_CHAR, ISLANDS, SITE_CHAR};

/// Bumped whenever generated maps change for the same seed and rules. Maps
/// without sites are unchanged by site placement, which draws nothing from
/// the generator when there are no sites to place.
pub const GENERATOR_VERSION: u32 = 1;

/// Generates the map for `seed` using `rules.c` for its layout, size, share
/// of water, number of neutral cities, and, with supply on, number of sites.
/// Rerolls deterministically until the map is valid.
pub fn generate(rules: &Rules, seed: u64) -> Result<Map, String> {
    let w = rules.c.map_width as i32;
    let h = rules.c.map_height as i32;
    if w < 8 || h < 6 || h % 2 != 0 {
        return Err("generated maps need map_width >= 8 and an even map_height >= 6".into());
    }
    if !rules.c.neutral_cities.is_multiple_of(2) {
        return Err("neutral_cities must be even, since generated cities come in mirrored pairs".into());
    }
    let sites = if rules.supply() { rules.c.sites } else { 0 };
    if !sites.is_multiple_of(2) {
        return Err("sites must be even, since generated sites come in mirrored pairs".into());
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
        site: if sites > 0 { terrain(SITE_CHAR)? } else { 0 },
    };
    let islands = rules.c.map_layout == ISLANDS;
    if islands {
        if w < 20 || h < 16 {
            return Err("the islands map needs map_width >= 20 and map_height >= 16".into());
        }
        if rules.c.neutral_cities != 2 {
            return Err("the islands map has a port on each home island: neutral_cities must be 2".into());
        }
        if sites < 4 {
            return Err("the islands map needs sites >= 4: one by each home, and a pair in the middle".into());
        }
        if !rules.naval() {
            return Err("the islands map needs ships".into());
        }
    }
    for attempt in 0..1000u64 {
        let mut rng = SplitMix64::new(mix64(seed ^ mix64(attempt.wrapping_add(1))));
        let map = if islands {
            try_islands(rules, &t, w, h, &mut rng)
        } else {
            try_generate(rules, &t, w, h, &mut rng)
        };
        if let Some(map) = map {
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
    /// Used only when there are sites to place.
    site: u8,
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

    let water_below = percentile(&elevation, rules.c.water_pct as usize * 10);
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
        sites: Vec::new(),
    };
    let passable = |tile: u8| rules.terrains[tile as usize].passable();

    // Home cities: player 1 near the top, player 2 at the mirror image.
    let home_row = rng.range(1, (h / 6).max(1));
    let home_col = rng.range(2, w - 3);
    let home = (home_row * w + home_col) as usize;
    let mut sites: Vec<usize> = vec![home, n - 1 - home];

    // Neutral cities in mirrored pairs, placed in the top half. The gap
    // between cities suits the default 6% water. With more water there's
    // less land to share, so the gap shrinks with the square root of the
    // land's share, and as many cities still fit.
    let gap = ((w + h) / 14).max(3) as u32;
    let min_gap = (gap * gap * (100 - rules.c.water_pct) / 94).isqrt().max(3);
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
    let resources = if rules.supply() && rules.c.sites > 0 {
        place_sites(rules, t, &map, &mut tiles, &sites, rng)?
    } else {
        Vec::new()
    };
    map.tiles = tiles;

    // Every city and site must be reachable from player 1's home. By
    // symmetry it is then reachable from player 2's home too.
    let targets: Vec<usize> = sites.iter().copied().chain(resources.iter().map(|&(k, _)| k)).collect();
    let reachable = if rules.naval() {
        reachable_by_land_and_sea(rules, &map, &sites, &targets)
    } else {
        reachable_by_land(rules, &map, sites[0], &targets)
    };
    if !reachable {
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
    map.sites = resources.into_iter().map(|(_, s)| s).collect();
    Some(map)
}

/// Value noise like `try_generate`'s, averaged with its mirror image so that
/// it's point-symmetric.
fn symmetric_noise(seed: u64, w: i32, n: usize, s: u32) -> Vec<u32> {
    let mut f: Vec<u32> = (0..n)
        .map(|k| {
            let (col, row) = ((k as i32 % w) as u32, (k as i32 / w) as u32);
            noise(seed, 8 * col + 4 * (row & 1), 7 * row, s)
        })
        .collect();
    for k in 0..n / 2 {
        let v = (f[k] + f[n - 1 - k]) / 2;
        f[k] = v;
        f[n - 1 - k] = v;
    }
    f
}

/// The islands map (STRATEGIC.md §3). Each home stands in the middle of an
/// island in its corner, with a site of its own next to it and a neutral
/// port to take on the coast. The middle of the map is an island with no
/// city, holding the other sites, the richest on the map, as one patch.
/// Everything else is sea. The seed shapes the coasts and the terrain, and
/// picks the port and the home sites. Returns None to reroll.
fn try_islands(rules: &Rules, t: &Palette, w: i32, h: i32, rng: &mut SplitMix64) -> Option<Map> {
    let n = (w * h) as usize;
    let c = &rules.c;
    let mut map = Map {
        width: w,
        height: h,
        tiles: vec![t.water; n],
        cities: Vec::new(),
        sites: Vec::new(),
    };
    let wobble = symmetric_noise(rng.next_u64(), w, n, 24);
    let relief = symmetric_noise(rng.next_u64(), w, n, 20);
    let moisture = symmetric_noise(rng.next_u64(), w, n, 28);

    // Player 1's home is near the top-left corner, player 2's at its mirror
    // image. The middle pair is the two hexes astride the middle of the map
    // that are each other's mirror images and next to each other.
    let home = ((h / 6 + rng.range(0, 1)) * w + w / 8 + rng.range(0, 1)) as usize;
    let row = (h / 2 - 1) as usize;
    let mid = (row * w as usize..(row + 1) * w as usize).find(|&k| map.hex(k).distance(map.hex(n - 1 - k)) == 1)?;
    let (home1, home2) = (map.hex(home), map.hex(n - 1 - home));
    let (mid1, mid2) = (map.hex(mid), map.hex(n - 1 - mid));
    let to_mid = |k: usize| map.hex(k).distance(mid1).min(map.hex(k).distance(mid2));

    // The islands: disks around the homes and the middle pair, their coasts
    // pushed in or out by up to a hex and a half. Homes and the middle pair
    // have land all around them.
    let (home_radius, mid_radius) = ((w.min(h) / 7).max(3) as i64, (w.min(h) / 10).max(2) as i64);
    let mut land: Vec<bool> = (0..n)
        .map(|k| {
            let at = map.hex(k);
            let push = wobble[k] as i64 - 512;
            let to_home = at.distance(home1).min(at.distance(home2)) as i64;
            1000 * to_home <= 1000 * home_radius + 3 * push || 1000 * to_mid(k) as i64 <= 1000 * mid_radius + 2 * push
        })
        .collect();
    for k in [home, n - 1 - home, mid, n - 1 - mid] {
        land[k] = true;
        for i in next_to(&map, k) {
            land[i] = true;
        }
    }

    // The sea is the water around the top-right corner, which must also take
    // in the bottom-left one, its mirror image. Any other water, such as a
    // lake, becomes land. Then any land but the three islands becomes sea.
    let (sea, _) = areas(&map, |k| !land[k]);
    let corner = (w - 1) as usize;
    if land[corner] || sea[corner] != sea[n - 1 - corner] {
        return None;
    }
    for k in 0..n {
        land[k] = land[k] || sea[k] != sea[corner];
    }
    let (isle, _) = areas(&map, |k| land[k]);
    let islands = [isle[home], isle[n - 1 - home], isle[mid]];
    if islands[0] == islands[1] || islands[0] == islands[2] || islands[1] == islands[2] {
        return None;
    }
    for k in 0..n {
        land[k] = land[k] && islands.contains(&isle[k]);
    }

    // Terrain: hills on the highest sixth of the land, forest on the wettest
    // quarter of the rest, and no mountains.
    let on_land = |f: &[u32]| -> Vec<u32> { (0..n).filter(|&k| land[k]).map(|k| f[k]).collect() };
    let hills_from = percentile(&on_land(&relief), 840);
    let forest_from = percentile(&on_land(&moisture), 750);
    let mut tiles: Vec<u8> = (0..n)
        .map(|k| {
            if !land[k] {
                t.water
            } else if relief[k] >= hills_from {
                t.hills
            } else if moisture[k] >= forest_from {
                t.forest
            } else {
                t.plains
            }
        })
        .collect();

    // Player 1's port: a coastal hex of its island at least 3 hexes from
    // home, among those nearest the middle. Its site: a hex next to home.
    let coast: Vec<usize> = (0..n)
        .filter(|&k| {
            land[k]
                && isle[k] == islands[0]
                && next_to(&map, k).any(|i| !land[i])
                && map.hex(k).distance(home1) >= 3
        })
        .collect();
    let nearest = coast.iter().map(|&k| to_mid(k)).min()?;
    let picks: Vec<usize> = coast.into_iter().filter(|&k| to_mid(k) <= nearest + 2).collect();
    let port = picks[rng.below(picks.len() as u64) as usize];
    let around: Vec<usize> = next_to(&map, home).collect();
    let site = around[rng.below(around.len() as u64) as usize];

    // The middle patch: the middle pair, then more mirrored pairs next to the
    // patch, nearest the middle first.
    let mut patch = vec![mid, n - 1 - mid];
    while patch.len() + 2 < c.sites as usize {
        let mut options: Vec<usize> = patch
            .iter()
            .flat_map(|&s| next_to(&map, s))
            .filter(|&k| land[k] && !patch.contains(&k))
            .collect();
        options.sort_unstable();
        options.dedup();
        let nearest = options.iter().map(|&k| to_mid(k)).min()?;
        options.retain(|&k| to_mid(k) == nearest);
        let k = options[rng.below(options.len() as u64) as usize];
        patch.extend([k, n - 1 - k]);
    }

    let cities = [home, n - 1 - home, port, n - 1 - port];
    for k in cities {
        tiles[k] = t.city;
    }
    let home_site = (
        (c.site_output_min + c.site_output_max) / 2,
        (c.site_cap_min + c.site_cap_max) / 2,
    );
    let mut sites = vec![(site, 1u8, home_site), (n - 1 - site, 2, home_site)];
    sites.extend(patch.iter().map(|&k| (k, 0, (c.site_output_max, c.site_cap_max))));
    for &(k, ..) in &sites {
        tiles[k] = t.site;
    }
    map.tiles = tiles;
    map.cities = cities
        .iter()
        .zip([1, 2, 0, 0])
        .map(|(&k, owner)| CityInfo { hex: map.hex(k), owner })
        .collect();
    map.sites = sites
        .iter()
        .map(|&(k, owner, (output, cap))| SiteInfo {
            hex: map.hex(k),
            owner,
            output,
            cap,
        })
        .collect();

    let targets: Vec<usize> = cities.iter().copied().chain(sites.iter().map(|s| s.0)).collect();
    reachable_by_land_and_sea(rules, &map, &cities, &targets).then_some(map)
}

/// The storage indexes of the hexes next to the one at index `k`.
fn next_to(map: &Map, k: usize) -> impl Iterator<Item = usize> + '_ {
    map.hex(k)
        .neighbors()
        .into_iter()
        .filter(|&h| map.contains(h))
        .map(|h| map.index(h))
}

/// Whether every hex in `targets` can be reached by land from the hex at
/// index `home`.
fn reachable_by_land(rules: &Rules, map: &Map, home: usize, targets: &[usize]) -> bool {
    let mut seen = vec![false; map.len()];
    let mut queue = vec![home];
    seen[home] = true;
    while let Some(k) = queue.pop() {
        for i in next_to(map, k) {
            if !seen[i] && rules.terrains[map.tiles[i] as usize].passable() {
                seen[i] = true;
                queue.push(i);
            }
        }
    }
    targets.iter().all(|&k| seen[k])
}

/// Labels the connected areas of the hexes `open` accepts: each such hex
/// gets its area's number, and every other hex `u32::MAX`. Also returns
/// the number of areas.
fn areas(map: &Map, open: impl Fn(usize) -> bool) -> (Vec<u32>, usize) {
    let mut area = vec![u32::MAX; map.len()];
    let mut count = 0;
    for start in 0..map.len() {
        if area[start] != u32::MAX || !open(start) {
            continue;
        }
        area[start] = count as u32;
        let mut stack = vec![start];
        while let Some(k) = stack.pop() {
            for i in next_to(map, k) {
                if area[i] == u32::MAX && open(i) {
                    area[i] = count as u32;
                    stack.push(i);
                }
            }
        }
        count += 1;
    }
    (area, count)
}

/// Whether every hex in `targets` can be reached from player 1's home, the
/// first of `cities`, by land and sea, with a port on the home's landmass
/// (STRATEGIC.md §3). Units walk across land and ride transports across
/// water. A sea is open to a player once they can reach a city next to it,
/// which could build the ships, and its coasts are open from then on.
fn reachable_by_land_and_sea(rules: &Rules, map: &Map, cities: &[usize], targets: &[usize]) -> bool {
    let terrain = |k: usize| &rules.terrains[map.tiles[k] as usize];
    let (land, lands) = areas(map, |k| terrain(k).passable());
    let (sea, seas) = areas(map, |k| terrain(k).naval_move_cost > 0);
    let home = land[cities[0]];
    let port = |c: usize| next_to(map, c).any(|i| sea[i] != u32::MAX);
    if !cities.iter().any(|&c| land[c] == home && port(c)) {
        return false;
    }
    let mut land_open = vec![false; lands];
    let mut sea_open = vec![false; seas];
    land_open[home as usize] = true;
    loop {
        let mut changed = false;
        for &c in cities {
            if land_open[land[c] as usize] {
                for i in next_to(map, c).filter(|&i| sea[i] != u32::MAX) {
                    changed |= !std::mem::replace(&mut sea_open[sea[i] as usize], true);
                }
            }
        }
        for k in (0..map.len()).filter(|&k| sea[k] != u32::MAX && sea_open[sea[k] as usize]) {
            for i in next_to(map, k).filter(|&i| land[i] != u32::MAX) {
                changed |= !std::mem::replace(&mut land_open[land[i] as usize], true);
            }
        }
        if !changed {
            break;
        }
    }
    targets
        .iter()
        .all(|&k| land[k] != u32::MAX && land_open[land[k] as usize])
}

/// `v` rounded down to a multiple of `step`, but at least `min`.
fn round_down(v: u32, step: u32, min: u32) -> u32 {
    (v / step * step).max(min)
}

/// A value in `lo..=hi` drawn for a site with `richness` in `0..=1000`: the
/// richer the site, the higher the value tends to be.
fn draw(lo: u32, hi: u32, richness: u32, rng: &mut SplitMix64) -> u32 {
    let frac = (richness + rng.below(1000) as u32) / 2;
    lo + ((hi - lo) as u64 * frac as u64 / 1000) as u32
}

/// Places the resource sites in mirrored pairs (STRATEGIC.md §3): one next
/// to each home, owned by that player; half the rest, rounded down, next to
/// neutral cities, at most one each; and the others in the open, at least 3
/// hexes from any city. The pair by the homes gets the middle of each range
/// of output and cap. Other pairs get richer the farther they are from the
/// nearer home, with output and cap drawn separately. Returns each site's
/// storage index and info in id order, or None to reroll the map.
fn place_sites(
    rules: &Rules,
    t: &Palette,
    map: &Map,
    tiles: &mut [u8],
    cities: &[usize],
    rng: &mut SplitMix64,
) -> Option<Vec<(usize, SiteInfo)>> {
    let n = tiles.len();
    let c = &rules.c;
    let pairs = (c.sites / 2) as usize;
    let gap = ((map.width + map.height) / 28).max(2) as u32;
    let hex = |k: usize| map.hex(k);
    let free = |tiles: &[u8], cells: &[usize], k: usize| {
        let (hk, hm) = (hex(k), hex(n - 1 - k));
        rules.terrains[tiles[k] as usize].passable()
            && tiles[k] != t.city
            && hk.distance(hm) >= 2
            && cells
                .iter()
                .all(|&s| hex(s).distance(hk) >= gap && hex(s).distance(hm) >= gap)
    };
    let next_to = |center: usize| -> Vec<usize> {
        hex(center)
            .neighbors()
            .into_iter()
            .filter(|&h| map.contains(h))
            .map(|h| map.index(h))
            .collect()
    };

    // The pair next to the homes. Homes are never boxed in, and no other
    // city is next to one, so there is always room.
    let around: Vec<usize> = next_to(cities[0])
        .into_iter()
        .filter(|&k| free(tiles, &[], k))
        .collect();
    if around.is_empty() {
        return None;
    }
    let k = around[rng.below(around.len() as u64) as usize];
    let mut cells = vec![k, n - 1 - k];

    // Pairs next to neutral cities. Each mirrored pair of cities is listed
    // by its first city, which is in the top half of the map.
    let mut towns: Vec<usize> = cities[2..].iter().step_by(2).copied().collect();
    let mut near = (pairs - 1) / 2;
    while near > 0 && !towns.is_empty() {
        let town = towns.swap_remove(rng.below(towns.len() as u64) as usize);
        let spots: Vec<usize> = next_to(town).into_iter().filter(|&k| free(tiles, &cells, k)).collect();
        if !spots.is_empty() {
            let k = spots[rng.below(spots.len() as u64) as usize];
            cells.extend([k, n - 1 - k]);
            near -= 1;
        }
    }

    // The rest in the open, in the top half of the map and their mirrors.
    let mut tries = 0;
    while cells.len() < 2 * pairs {
        tries += 1;
        if tries > 8000 {
            return None;
        }
        let k = rng.below((n / 2) as u64) as usize;
        let hk = hex(k);
        if free(tiles, &cells, k)
            && hk.distance(hex(n - 1 - k)) >= gap
            && cities.iter().all(|&s| hex(s).distance(hk) >= 3)
        {
            cells.extend([k, n - 1 - k]);
        }
    }

    let (home1, home2) = (hex(cities[0]), hex(cities[1]));
    let half = (home1.distance(home2) / 2).max(1);
    let mut out = Vec::with_capacity(cells.len());
    for (i, pair) in cells.chunks(2).enumerate() {
        let (output, cap) = if i == 0 {
            (
                (c.site_output_min + c.site_output_max) / 2,
                (c.site_cap_min + c.site_cap_max) / 2,
            )
        } else {
            let at = hex(pair[0]);
            let richness = (at.distance(home1).min(at.distance(home2)) * 1000 / half).min(1000);
            let output = draw(c.site_output_min, c.site_output_max, richness, rng);
            let cap = draw(c.site_cap_min, c.site_cap_max, richness, rng);
            (
                round_down(output, 5, c.site_output_min),
                round_down(cap, 50, c.site_cap_min),
            )
        };
        for (j, &k) in pair.iter().enumerate() {
            tiles[k] = t.site;
            let owner = if i == 0 { j as u8 + 1 } else { 0 };
            out.push((
                k,
                SiteInfo {
                    hex: hex(k),
                    owner,
                    output,
                    cap,
                },
            ));
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hex::Hex;

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

    fn site_rules(w: u32, h: u32, cities: u32, sites: u32) -> Rules {
        let mut rules = Rules::default();
        rules.set("supply", "1").unwrap();
        rules.set("terrain.R", "site 1 0 0 0").unwrap();
        (rules.c.map_width, rules.c.map_height) = (w, h);
        (rules.c.neutral_cities, rules.c.sites) = (cities, sites);
        rules
    }

    #[test]
    fn sites_come_in_mirrored_pairs() {
        for (rules, seeds) in [(site_rules(40, 30, 10, 12), 0..6), (site_rules(80, 60, 46, 32), 0..3)] {
            for seed in seeds {
                let m = generate(&rules, seed).unwrap();
                assert_eq!(m, generate(&rules, seed).unwrap());
                m.validate(&rules).unwrap();
                let n = m.len();
                assert!((0..n).all(|k| m.tiles[k] == m.tiles[n - 1 - k]), "seed {seed}");
                assert_eq!(m.sites.len(), rules.c.sites as usize);
                for pair in m.sites.chunks(2) {
                    assert_eq!(m.mirror(pair[0].hex), pair[1].hex);
                    assert_eq!((pair[0].output, pair[0].cap), (pair[1].output, pair[1].cap));
                }
                assert_eq!((m.sites[0].owner, m.sites[1].owner), (1, 2));
                assert_eq!(m.sites[0].hex.distance(m.cities[0].hex), 1);
                assert_eq!((m.sites[0].output, m.sites[0].cap), (60, 1700));
                for s in &m.sites[2..] {
                    assert_eq!(s.owner, 0);
                    assert!((20..=100).contains(&s.output) && (400..=3000).contains(&s.cap), "{s:?}");
                }
                // Half the other pairs, rounded down, sit next to a neutral city.
                let near = m.sites[2..]
                    .iter()
                    .filter(|s| m.cities.iter().any(|c| c.hex.distance(s.hex) == 1))
                    .count();
                assert_eq!(near, 2 * ((rules.c.sites as usize / 2 - 1) / 2), "seed {seed}");
            }
        }
        // Small maps work too.
        let rules = site_rules(16, 12, 4, 4);
        for seed in 0..20 {
            generate(&rules, seed).unwrap().validate(&rules).unwrap();
        }
    }

    /// The strategic ruleset's ships on a map with `water_pct` water.
    fn naval_rules(w: u32, h: u32, cities: u32, sites: u32, water_pct: u32) -> Rules {
        let mut rules = site_rules(w, h, cities, sites);
        rules.set("water_pct", &water_pct.to_string()).unwrap();
        rules.set("terrain.water.naval_move_cost", "1").unwrap();
        rules
            .set("unit.T", "transport 150 100 6 2 1 1 naval,transport")
            .unwrap();
        rules.validate().unwrap();
        rules
    }

    #[test]
    fn naval_maps_have_their_water_and_reach_every_city_by_land_and_sea() {
        for (rules, seeds) in [
            (naval_rules(80, 60, 46, 32, 40), 0..3),
            (naval_rules(24, 18, 6, 4, 40), 0..12),
        ] {
            let mut islands = 0;
            for seed in seeds {
                let m = generate(&rules, seed).unwrap();
                assert_eq!(m, generate(&rules, seed).unwrap());
                m.validate(&rules).unwrap();
                let n = m.len();
                assert!((0..n).all(|k| m.tiles[k] == m.tiles[n - 1 - k]), "seed {seed}");
                let water = m
                    .tiles
                    .iter()
                    .filter(|&&t| rules.terrains[t as usize].ch == '~')
                    .count();
                assert!(
                    (n * 35 / 100..=n * 41 / 100).contains(&water),
                    "seed {seed}: {water} of {n}"
                );
                let cities: Vec<usize> = m.cities.iter().map(|c| m.index(c.hex)).collect();
                let targets: Vec<usize> = cities
                    .iter()
                    .copied()
                    .chain(m.sites.iter().map(|s| m.index(s.hex)))
                    .collect();
                assert!(reachable_by_land_and_sea(&rules, &m, &cities, &targets), "seed {seed}");
                if !reachable_by_land(&rules, &m, cities[0], &targets) {
                    islands += 1;
                }
            }
            assert!(islands > 0, "some maps should need ships");
        }
    }

    /// A map from rows of terrain characters, with no cities or sites.
    fn sketch(rules: &Rules, rows: &[&str]) -> Map {
        let tiles: Vec<u8> = rows
            .iter()
            .flat_map(|r| r.chars().map(|c| rules.terrain_index(c).unwrap() as u8))
            .collect();
        Map {
            width: rows[0].len() as i32,
            height: rows.len() as i32,
            tiles,
            cities: Vec::new(),
            sites: Vec::new(),
        }
    }

    #[test]
    fn the_sea_check_needs_a_port_at_home_and_a_sea_that_goes_somewhere() {
        let rules = naval_rules(8, 6, 0, 0, 40);
        // A sea between two landmasses. The city at (0,1) is a port, and
        // the one at (7,0) isn't.
        let m = sketch(
            &rules,
            &[".......C", "C.......", "~~~~~~~~", "~~~~~~~~", "........", ".......C"],
        );
        let at = |q: i32, r: i32| m.index(Hex::new(q, r));
        let (port, inland, south) = (at(0, 1), at(7, 0), at(5, 5));
        assert!(reachable_by_land_and_sea(&rules, &m, &[port, south], &[south]));
        assert!(!reachable_by_land_and_sea(&rules, &m, &[inland, south], &[south]));
        assert!(reachable_by_land_and_sea(&rules, &m, &[inland, port, south], &[south]));
        assert!(!reachable_by_land(&rules, &m, port, &[south]));
        // A lake that touches only the home landmass leads nowhere.
        let m = sketch(
            &rules,
            &["C..C~...", "........", "^^^^^^^^", "........", "........", ".......C"],
        );
        let at = |q: i32, r: i32| m.index(Hex::new(q, r));
        let cities = [at(0, 0), at(3, 0), at(5, 5)];
        assert!(!reachable_by_land_and_sea(&rules, &m, &cities, &[cities[2]]));
        assert!(reachable_by_land_and_sea(&rules, &m, &cities, &[cities[1]]));
    }

    /// The islands map's rules, with ships, at the given size and number of
    /// sites.
    fn island_rules(w: u32, h: u32, sites: u32) -> Rules {
        let mut rules = naval_rules(w, h, 2, sites, 6);
        rules.set("map_layout", "islands").unwrap();
        rules.validate().unwrap();
        rules
    }

    #[test]
    fn islands_maps() {
        for (rules, seeds) in [
            (island_rules(40, 30, 4), 0..12),
            (island_rules(20, 16, 4), 0..12),
            (island_rules(41, 30, 6), 0..6),
            (island_rules(80, 60, 10), 0..3),
        ] {
            for seed in seeds {
                let m = generate(&rules, seed).unwrap();
                assert_eq!(m, generate(&rules, seed).unwrap());
                m.validate(&rules).unwrap();
                let n = m.len();
                assert!((0..n).all(|k| m.tiles[k] == m.tiles[n - 1 - k]), "seed {seed}");
                let water = |h: Hex| m.contains(h) && rules.terrains[m.terrain_at(h)].ch == '~';
                let port = |h: Hex| h.neighbors().into_iter().any(water);
                let (land, _) = areas(&m, |k| rules.terrains[m.tiles[k] as usize].passable());
                let isle = |h: Hex| land[m.index(h)];

                // The homes, then a neutral port on each home's island. A
                // home isn't a port.
                let owners: Vec<u8> = m.cities.iter().map(|c| c.owner).collect();
                assert_eq!(owners, [1, 2, 0, 0]);
                for pair in m.cities.chunks(2) {
                    assert_eq!(m.mirror(pair[0].hex), pair[1].hex);
                }
                let (home, port1) = (m.cities[0].hex, m.cities[2].hex);
                assert!(!port(home) && port(port1), "seed {seed}");
                assert!(home.distance(port1) >= 3);
                assert_eq!(isle(home), isle(port1));

                // Each home's site next to it, then the middle patch: the
                // richest sites, touching, on an island of their own.
                assert_eq!(m.sites.len(), rules.c.sites as usize);
                for pair in m.sites.chunks(2) {
                    assert_eq!(m.mirror(pair[0].hex), pair[1].hex);
                }
                assert_eq!((m.sites[0].owner, m.sites[1].owner), (1, 2));
                assert_eq!(m.sites[0].hex.distance(home), 1);
                assert_eq!((m.sites[0].output, m.sites[0].cap), (60, 1700));
                let middle = isle(m.sites[2].hex);
                assert!(middle != isle(home) && middle != isle(m.cities[1].hex));
                assert_eq!(m.sites[2].hex.distance(m.sites[3].hex), 1);
                for s in &m.sites[2..] {
                    assert_eq!((s.owner, s.output, s.cap), (0, 100, 3000));
                    assert_eq!(isle(s.hex), middle);
                    assert!(m.sites[2..].iter().any(|o| o.hex.distance(s.hex) == 1));
                }
                assert!(m.cities.iter().all(|c| isle(c.hex) != middle));

                // Everything is reachable, but only by sea.
                let cities: Vec<usize> = m.cities.iter().map(|c| m.index(c.hex)).collect();
                let targets: Vec<usize> = cities
                    .iter()
                    .copied()
                    .chain(m.sites.iter().map(|s| m.index(s.hex)))
                    .collect();
                assert!(reachable_by_land_and_sea(&rules, &m, &cities, &targets), "seed {seed}");
                assert!(reachable_by_land(&rules, &m, cities[0], &targets[4..5]));
                assert!(!reachable_by_land(&rules, &m, cities[0], &targets[6..7]));
            }
        }
        let rules = island_rules(40, 30, 4);
        assert_ne!(generate(&rules, 1).unwrap(), generate(&rules, 2).unwrap());
    }

    #[test]
    fn islands_need_two_ports_a_middle_pair_and_ships() {
        let mut rules = island_rules(40, 30, 4);
        rules.c.neutral_cities = 4;
        assert!(generate(&rules, 1).is_err());
        let mut rules = island_rules(40, 30, 4);
        rules.c.sites = 2;
        assert!(generate(&rules, 1).is_err());
        assert!(generate(&island_rules(16, 12, 4), 1).is_err());
        let mut rules = site_rules(40, 30, 2, 4);
        rules.c.map_layout = ISLANDS;
        assert!(generate(&rules, 1).is_err());
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
