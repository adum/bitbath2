# Bitbath

A turn-based, two-player strategy game on a hex map, played by programs. Bots talk to the engine over stdin and stdout, so they can be written in any language. Bitbath is first an LLM benchmark for strategic bot programming, and second a game for people who like writing bots. [SPEC.md](SPEC.md) has the full design.

Status: milestones M1–M3 are built. That covers the engine, the `bitbath` runner with a Python starter kit, and a browser replay viewer. The reference bots include random, greedy, and solid opponents.

## Quick start

You need Rust and Python 3. The viewer also needs Rust's wasm target: `rustup target add wasm32-unknown-unknown`.

The commands below work as written in PowerShell, Git Bash, and Linux or macOS shells. In the Windows Command Prompt, write the program path with backslashes, as in `target\release\bitbath`. On Linux and macOS, you may need `python3` instead of `python`.

```bash
cargo build --release
target/release/bitbath play --p1 "python bots/solid_bot.py" --p2 "python bots/greedy_bot.py" --map seed:42
```

Each game gets a directory under `runs/` with its replay, `replay.bbr`, and a working directory for each bot.

```bash
target/release/bitbath replay runs/<game>/replay.bbr --text
```

`replay` re-simulates the recorded orders with the engine and checks the result. Add `--text` for a turn-by-turn summary, or `--orders` to also print every order and its reply.

To watch a game, build the viewer once:

```bash
python scripts/build_web.py
```

Then open `web/index.html` in a browser and choose a replay file, or drop one on the page. If you serve the repository over HTTP, `web/index.html?replay=/runs/<game>/replay.bbr` opens a replay directly. In the viewer:

- **Space** plays or pauses.
- **← / →** step through orders.
- **↑ / ↓** jump between turns.
- **Scroll** to zoom, and **drag** to pan.

## Writing a bot

The Python kit in [kits/python/bitbath.py](kits/python/bitbath.py) handles the protocol, tracks the game state, and provides hex math, pathfinding, and a combat predictor that matches the engine:

```python
import bitbath as bb

class MyBot(bb.Bot):
    def on_turn(self, state):
        targets = [c.hex for c in state.cities if c.owner != self.player]
        field = self.distance_field(targets)
        for unit in state.my_units():
            if not self.can_capture(unit):
                self.move_toward(unit, field)
            if self.can_capture(unit):
                self.capture(unit)
        for city in state.my_cities():
            if state.gold >= self.rules.unit_types["I"].cost:
                self.build(city, "I")

bb.run(MyBot())
```

[bots/greedy_bot.py](bots/greedy_bot.py) is a fuller example, including debug annotations that the viewer draws. [bots/smart_bot.py](bots/smart_bot.py) is a stronger heuristic bot that beats it: it focuses fire, masses units before attacking, and picks its builds from the damage table. A bot in another language implements the protocol in SPEC §6 directly. The golden test vectors in [tests/vectors](tests/vectors) check hex math and damage.

[bots/solid_bot.py](bots/solid_bot.py) coordinates capturers, scores attacks from reachable hexes, and recruits first at threatened cities or near the front. It caches terrain distances to each city during setup to keep turns quick.

## Rules and maps

- `--map seed:<n>` generates a point-symmetric map.
- `--map <file>` loads a hand-made map, written as `MAP`, `ROW`, and `CITY` lines. `bitbath map --map seed:<n> --lines` prints one to start from.
- `--config <file>` overrides rules. [configs/default.toml](configs/default.toml) lists the keys, and [configs/small.toml](configs/small.toml) is a short game on a small map.
- `--set key=value` overrides a single rule.

## Repository layout

```
engine/         Rust: rules, map generation, vision, order processing. No I/O.
runner/         Rust: the bitbath command (bot processes, clocks, replays).
wasm/           The engine compiled to wasm for the viewer.
web/            The replay viewer.
kits/python/    Python starter kit and its tests.
bots/           Reference bots: random_bot.py, greedy_bot.py, solid_bot.py.
tests/vectors/  Golden test vectors shared by the engine and kits.
configs/        Rule presets.
scripts/        Viewer build and test vector generator.
```

## Tests

```bash
cargo test --workspace
python kits/python/test_bitbath.py
```
