//! SplitMix64: a tiny generator whose output is fixed forever, unlike
//! `rand::StdRng` (SPEC §3.9). It is used only by map generation.

pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    pub fn new(seed: u64) -> SplitMix64 {
        SplitMix64 { state: seed }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        mix64(self.state)
    }

    /// A value in `0..n`. `n` must be positive. Plain modulo: the slight bias
    /// doesn't matter, only that the output never changes.
    pub fn below(&mut self, n: u64) -> u64 {
        self.next_u64() % n
    }

    /// A value in `lo..=hi`.
    pub fn range(&mut self, lo: i32, hi: i32) -> i32 {
        lo + self.below((hi - lo + 1) as u64) as i32
    }
}

/// The SplitMix64 output function, used to hash seeds and lattice points.
pub fn mix64(x: u64) -> u64 {
    let mut z = x;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_values() {
        // Reference outputs of SplitMix64 seeded with 0.
        let mut rng = SplitMix64::new(0);
        assert_eq!(rng.next_u64(), 0xE220_A839_7B1D_CDAF);
        assert_eq!(rng.next_u64(), 0x6E78_9E6A_A1B9_65F4);
    }
}
