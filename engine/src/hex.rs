//! Hex math on pointy-top hexes in axial coordinates (SPEC §3.1).

/// A hex in axial coordinates. `r` is the row, counting down from 0 at the
/// top; `q` runs along the row.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Hex {
    pub q: i32,
    pub r: i32,
}

/// Direction offsets `(Δq, Δr)`, numbered counter-clockwise from east:
/// E, NE, NW, W, SW, SE.
pub const DIRS: [(i32, i32); 6] = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)];

impl Hex {
    pub const fn new(q: i32, r: i32) -> Hex {
        Hex { q, r }
    }

    /// The neighbor in direction `dir` (0..6).
    pub fn step(self, dir: usize) -> Hex {
        let (dq, dr) = DIRS[dir];
        Hex::new(self.q + dq, self.r + dr)
    }

    /// The six neighbors, in direction order.
    pub fn neighbors(self) -> [Hex; 6] {
        let mut out = [self; 6];
        for (dir, h) in out.iter_mut().enumerate() {
            *h = self.step(dir);
        }
        out
    }

    pub fn distance(self, other: Hex) -> u32 {
        let dq = self.q - other.q;
        let dr = self.r - other.r;
        ((dq.abs() + dr.abs() + (dq + dr).abs()) / 2) as u32
    }

    /// Every hex within `radius` of this one, row by row, then by `q`.
    pub fn disk(self, radius: u32) -> impl Iterator<Item = Hex> {
        let n = radius as i32;
        (-n..=n).flat_map(move |dr| {
            let lo = (-n).max(-dr - n);
            let hi = n.min(-dr + n);
            (lo..=hi).map(move |dq| Hex::new(self.q + dq, self.r + dr))
        })
    }
}

impl std::fmt::Display for Hex {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "({},{})", self.q, self.r)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn distance_and_neighbors() {
        let h = Hex::new(2, 3);
        for n in h.neighbors() {
            assert_eq!(h.distance(n), 1);
        }
        assert_eq!(Hex::new(0, 0).distance(Hex::new(3, -1)), 3);
        assert_eq!(Hex::new(-2, 5).distance(Hex::new(1, 1)), 4);
    }

    #[test]
    fn disk_sizes() {
        for r in 0..5u32 {
            let n = Hex::new(4, 4).disk(r).count() as u32;
            assert_eq!(n, 3 * r * (r + 1) + 1);
            assert!(Hex::new(4, 4).disk(r).all(|h| h.distance(Hex::new(4, 4)) <= r));
        }
    }
}
