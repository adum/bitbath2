//! Bitbath game engine: rules, map generation, vision, and order processing.
//!
//! The engine is pure. It has no I/O, clock, threads, floating point, or
//! hash-ordered iteration, so the same orders always produce the same game
//! (SPEC §12). The runner, the replay tools, and the browser viewer all drive
//! the same [`Game`].

pub mod game;
pub mod hex;
pub mod map;
pub mod mapgen;
pub mod rng;
pub mod rules;

pub use game::{damage, City, Game, Outcome, Site, Unit};
pub use hex::Hex;
pub use map::{parse_setup, Map};
pub use rules::Rules;

/// Protocol version sent in the `BITBATH` line (SPEC §6.2).
pub const PROTOCOL_VERSION: u32 = 1;

/// Engine version, recorded in replay headers.
pub const ENGINE_VERSION: &str = env!("CARGO_PKG_VERSION");
