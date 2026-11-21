# Bitbath: build a bot

Bitbath is a turn-based, two-player strategy game on a hex map, played by programs. This is a competition, and your task is to write the strongest bot you can. When you're done, your bot plays a tournament against bots that others built from this same kit and against reference bots, four of which aren't in the kit. Its rating in that tournament is the result.

[SPEC.md](SPEC.md) is the game's full design. Sections 3 to 8 are what a bot author needs: the rules, fog of war, the protocol, time control, and replays.

## What you submit

Everything in `submission/`, and nothing else. It starts out holding a placeholder bot that works.

- `submission/bot.toml` gives the `run` command that starts your bot and, if the bot needs building, the `build` command.
- The directory has to run on its own. Your bot can't use files outside it, which is why it holds its own copy of the Python kit.
- It may hold at most 20 MB before it's built. Build output that you leave in it counts, so delete that before you finish.
- Any language that's installed here is allowed.

Check it before you finish:

```bash
target/release/bitbath check submission
```

This builds your bot, plays it against itself, and fails if either side crashes, hangs, or doesn't start. A bot that fails this check can't enter the tournament.

## The tournament

- **Opponents.** Every bot plays every other bot: the bots others built, the six sample bots in `bots/`, and four stronger reference bots that aren't in the kit. Apart from the bots in `bots/`, you can't see the other bots, and none of them can see yours.
- **Games.** Each pair of bots plays each map twice, with the sides swapped.
- **Maps.** Every game is on a map like those of `configs/micro.toml`: 12 by 8 hexes, with two neutral cities and an 80-round limit. The map seeds are ones you haven't seen.
- **Rules.** The rules are those of `configs/micro.toml`, which changes only the map from the defaults in `configs/default.toml`. The engine sends the rules at the start of each game, so read them from that message instead of writing them into your bot.
- **Time.** The clock in SPEC §7 is enforced. A bot that crashes, hangs, or runs out of memory forfeits the game.
- **Isolation.** Your bot has no network. It starts each game in an empty working directory, so nothing it writes carries over from one game to the next.
- **Scoring.** Bots are rated from all the games together. A draw counts as half a win.

## Working here

Build the runner once, if `target/release/bitbath` isn't there already:

```bash
cargo build --release
```

Play a game, and read how it went:

```bash
target/release/bitbath play --p1 submission --p2 "python bots/greedy_bot.py" --config configs/micro.toml --map seed:7
target/release/bitbath replay runs/<game>/replay.bbr --text
```

Each game gets a directory under `runs/`, with its replay and a working directory for each bot. The replay holds what your bot wrote to stderr, turn by turn.

Run a tournament of your own, to compare versions of your bot against each other and the kit's bots:

```bash
target/release/bitbath tournament --bot mine=submission --bot greedy="python bots/greedy_bot.py" --bot rush="python bots/rush_bot.py" --bot turtle="python bots/turtle_bot.py" --config configs/micro.toml --seeds 1-10 --jobs 4
```

`target/release/bitbath help` lists every command and option.

## What's in the kit

```
submission/     Your bot. This is the only directory that is submitted.
SPEC.md         The game's design: rules, protocol, time control.
engine/         The game engine, in Rust. It is the final word on the rules.
runner/         The bitbath command: games, tournaments, ratings, replays.
kits/python/    The Python kit: protocol, game state, hex math, pathfinding,
                and a combat predictor that matches the engine.
bots/           Six sample bots. random_bot.py plays legal moves, and
                greedy_bot.py is a fuller example of the Python kit. The
                other four each play one style: turtle_bot.py defends its
                half, volley_bot.py masses archers, raider_bot.py hunts
                with cavalry, and rush_bot.py attacks at once.
configs/        Rule presets.
tests/vectors/  Test vectors for hex math, damage, vision, and orders, for
                checking a bot written without the kit.
```

## How strong the opposition is

All six bots in `bots/` play in the tournament. With greedy's rating fixed at 1000, the reference bots are rated about:

| Bot | Rating |
|---|---|
| the four reference bots that aren't in the kit | 1250 to 1600 |
| greedy | 1000 |
| rush | 800 |
| volley, raider | 750 |
| turtle | 420 |
| random | 330 |

A gap of 250 points means winning about four games in five. The weakest hidden bot wins about four games in five against greedy, and the strongest nearly every game.

- **The six sample bots are for practice.** Each tests your bot against a different way of playing. Beating them is where the work starts, and a bot that only edges out greedy finishes in the bottom half.
- **Winning four games in five against greedy is the first real milestone.** That is the level of the weakest hidden bot.
- **Tuning a sample bot's numbers isn't enough.** A copy of a sample bot scores about what that bot scores, with or without a few constants changed. Getting further takes a design of your own.
- **The hidden bots can be beaten.** They are hand-written heuristics, and none is close to perfect play.
