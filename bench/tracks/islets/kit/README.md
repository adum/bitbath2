# Bitbath: build a bot

Bitbath is a turn-based, two-player strategy game on a hex map, played by programs. This is a competition, and your task is to write the strongest bot you can for one of its maps, Islets. Islets is played under the strategic rules: armies eat bread that wagons haul from resource sites, and they cross the sea in ships. When you're done, your bot plays a tournament against bots that others built from this same kit and against reference bots, four of which aren't in the kit. Its rating in that tournament is the result.

Two documents hold the rules:

- [SPEC.md](SPEC.md) is the game's full design. Sections 3 to 8 are what a bot author needs: the rules, fog of war, the protocol, time control, and replays.
- [STRATEGIC.md](STRATEGIC.md) is the strategic ruleset, which changes those rules and adds to them. Sections 3 to 11 cover the map, wheat and bread, food, ships, the unit types, how a game is won, and the protocol's new lines and orders. Section 3 describes the Islets map.

[configs/islets.toml](configs/islets.toml) holds every number of the game you're building for.

## What you submit

Everything in `submission/`, and nothing else. It starts out holding a placeholder bot that works.

- `submission/bot.toml` gives the `run` command that starts your bot and, if the bot needs building, the `build` command.
- The directory has to run on its own. Your bot can't use files outside it, which is why it holds its own copy of the Python kit.
- It may hold at most 20 MB before it's built. Build output that you leave in it counts, so delete that before you finish.
- Any language that's installed here is allowed.

Check it before you finish:

```bash
target/release/bitbath check submission --config configs/islets.toml
```

This builds your bot, plays it against itself on an Islets map, and fails if either side crashes, hangs, or doesn't start. A bot that fails this check can't enter the tournament.

## The tournament

- **Opponents.** Every bot plays every other bot: the bots others built, the three bots in `bots/` that play this map, and four stronger reference bots that aren't in the kit. Apart from the bots in `bots/`, you can't see the other bots, and none of them can see yours.
- **Games.** Each pair of bots plays each map twice, with the sides swapped.
- **Maps.** Every game is on a map like those of `configs/islets.toml`: 24 by 18 hexes, mostly sea, with a 300-round limit. Each player starts on an island in its own corner, with a resource site near its home city and a neutral port on its coast. Only a port builds ships. A third island in the middle has no city and the two richest sites. The map seeds are ones you haven't seen.
- **Rules.** The rules are those of `configs/islets.toml`. The engine sends the rules at the start of each game, so read them from that message instead of writing them into your bot.
- **Time.** The clock in SPEC §7 is enforced, with this preset's numbers: a bank of 10 seconds to start with, 2 seconds more each turn, and at most 20 seconds banked. A bot that crashes, hangs, or runs out of memory forfeits the game.
- **Isolation.** Your bot has no network. It starts each game in an empty working directory, so nothing it writes carries over from one game to the next.
- **Scoring.** Bots are rated from all the games together, with supply's rating fixed at 1000. A draw counts as half a win.

## Working here

Build the runner once, if `target/release/bitbath` isn't there already:

```bash
cargo build --release
```

Play a game, and read how it went:

```bash
target/release/bitbath play --p1 submission --p2 "python bots/naval_bot.py" --config configs/islets.toml --map seed:7
target/release/bitbath replay runs/<game>/replay.bbr --text
```

Each game gets a directory under `runs/`, with its replay and a working directory for each bot. The replay holds what your bot wrote to stderr, turn by turn.

Run a tournament of your own, to compare versions of your bot against each other and the kit's bots:

```bash
target/release/bitbath tournament --bot mine=submission --bot naval="python bots/naval_bot.py" --bot supply="python bots/supply_bot.py" --bot forager=target/release/forager_bot --config configs/islets.toml --seeds 1-10 --jobs 4
```

Without `--config configs/islets.toml`, these commands play the default game, which has other rules and another map.

`target/release/forager_bot` is built from `bots/forager_bot.rs`. Should you need to build it again:

```bash
rustc -O bots/forager_bot.rs -o target/release/forager_bot
```

`target/release/bitbath help` lists every command and option.

## What's in the kit

```
submission/     Your bot. This is the only directory that is submitted.
SPEC.md         The game's design: rules, protocol, time control.
STRATEGIC.md    The strategic ruleset: supply, ships, and the Islets map.
engine/         The game engine, in Rust. It is the final word on the rules.
runner/         The bitbath command: games, tournaments, ratings, replays.
kits/python/    The Python kit: protocol, game state, hex math, pathfinding,
                and a combat predictor that matches the engine. It knows the
                strategic rules.
bots/           Three bots that play this map, and a file they build on:
                  naval_bot.py     hauls wheat with wagons, ships its army to
                                   other islands, and raids with galleys.
                  supply_bot.py    hauls wheat and feeds its army, and builds
                                   no ships. Naval is built on it.
                  forager_bot.rs   a bot in one Rust file that doesn't use the
                                   kit. It is built already, as
                                   target/release/forager_bot.
                  vanguard_bot.py  fights on land. Supply is built on it. It
                                   doesn't know the strategic rules, and isn't
                                   in the tournament.
configs/        Rule presets. islets.toml is the one every game is played
                with. STRATEGIC.md and SPEC.md describe it by the others.
tests/vectors/  Test vectors for hex math, damage, vision, orders, supply, and
                ships, for checking a bot written without the kit.
```

## How strong the opposition is

Naval, supply, and forager all play in the tournament, and so do the four reference bots that aren't in the kit. These are their results against each other, in 40 games a pair: 20 maps, each played from both sides.

| Pair | Games won |
|---|---|
| each of the four hidden bots against naval | 40 to 0 |
| the strongest three hidden bots against the fourth | 115 to 5 |
| naval against supply, and against forager | 40 to 0 each |
| supply against forager | 30 to 10 |

- **Results on this map are lopsided.** In 16 of the 21 pairs among these seven bots, one bot won all 40 games.
- **The bots in `bots/` are for practice.** Beating them is where the work starts, and a bot that only beats naval finishes in the bottom half.
- **Beating naval in every game is the first real milestone.** Each hidden bot does that.
- **A copy of naval scores what naval scores:** no wins against the four hidden bots.
- **The hidden bots can be beaten.** They are hand-written heuristics, and none is close to perfect play.
