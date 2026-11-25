# Bitbath

A turn-based, two-player strategy game on a hex map, played by programs. Bots talk to the engine over stdin and stdout, so they can be written in any language. Bitbath is first an LLM benchmark for strategic bot programming, and second a game for people who like writing bots. [SPEC.md](SPEC.md) has the full design.

Status: milestones M1–M3 are built. That covers the engine, the `bitbath` runner with a Python starter kit, and a browser replay viewer, which also lets you play against a built-in bot. The reference bots include random, greedy, and solid opponents.

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

Then open `web/play.html` in a browser and choose a replay file, or drop one on the page. To open replays by URL instead, serve the repository with `python scripts/serve.py` and open `http://localhost:8000/web/play.html?replay=/runs/<game>/replay.bbr`. After rebuilding the viewer, reload the page; the viewer warns when a replay doesn't play back the way it was recorded, which usually means the viewer is out of date. In the viewer:

- **Space** plays or pauses.
- **← / →** step through orders.
- **↑ / ↓** jump between turns.
- **Scroll** to zoom, and **drag** to pan.

## Playing by hand

Open [the Bitbath homepage](web/index.html) after running `python scripts/build_web.py`. It shows the latest Micro standings, with filters for LLM entries and the full field. **Play Micro** opens match setup on the full-window [Play page](web/play.html). Completing an arena run or rebuilding the web engine refreshes the standings from local results. A fresh checkout includes the last exported standings.

The viewer's page also plays games: you against a bot built into the page. Click **New game**, then pick an opponent, a map size, and a side. The `random` bot is for learning the controls, and `greedy` puts up a fight. Bastion, Smart, and Vanguard run their tournament Python code in a browser worker. Start the local web server or use the hosted site to play them.

The hosted site records games as you play. Enter a player name in **New game** to appear on the human scoreboard. Names are remembered on this browser and matching names share a score. Guests contribute to site totals. The [human scoreboard](web/humans.html) links to every recent game. Unfinished games show city counts and army values and open at the latest saved move. Their results count toward rank and win rate once the game finishes.

To run the scoreboard locally with Node 22.13 or newer, build the hosted site with `python scripts/build_web.py --dist`. In `site/`, run `npm ci`, `npx wrangler d1 migrations apply DB --local`, and `npm run dev`. Run `npm test` there to check recording and scoreboard behavior.

You see the game through your own fog of war:

- **Click** one of your units to select it. The hexes it can reach are tinted, and the enemies it can attack are ringed.
- **Click a tinted hex** to move there, or **a ringed enemy** to attack it. Hovering shows the path, or what the attack would do.
- **Right-click** (or Shift-click) moves onto a hex that holds your own units; a plain click selects one of them instead. A right-click anywhere else deselects.
- **C** captures the city the unit stands on.
- **Click one of your cities** to build there, or press a unit's letter, such as **I** for infantry. A gold **+** marks each city that can still build this turn.
- **N** or **Tab** selects the next unit with something left to do, **Esc** deselects, and **E** or **Enter** ends your turn.
- **Rest the pointer** on a hex for a card beside it: the ground, the city or site, and each unit there with its hit points and what it carries. Replays show it too.

**Settings**, at the top right, picks how units are drawn: a symbol for each kind (a sword for infantry, pikes, a horse's head, a bow, an eye for the scout), or its letter. It also has the volume of the sound effects: short, quiet sounds for moves, fights, captures, builds, and the turn changing hands, in a game you play. At 0 they are off. Both choices are kept in the browser.

A unit that is dug in carries a white block on its top edge for each fortification level, yours and the opponent's alike, in games and in replays.

The maps marked **ships and food** play the strategic ruleset ([STRATEGIC.md](STRATEGIC.md)): bread in place of gold, sites, wagons, rations, and ships. Islets is the small one to learn on, the islands map at 24 × 18 ([configs/islets.toml](configs/islets.toml)), Islands is [configs/islands.toml](configs/islands.toml), Seas is a mid-sized map of land and sea, and Strategic is [configs/strategic.toml](configs/strategic.toml) in full. The built-in opponent for them is Naval, which is [bots/naval_bot.py](bots/naval_bot.py) in a browser worker; the bots on disk are offered too. What these rules add to the controls:

- **Bread** is kept by each city. The number at the foot of a city's hex is its bread, and a site's is its wheat, for every city and site you can see. Selecting a city shows what it can build with its own bread; only a port, a city next to water, builds ships.
- **Sites** have no owners, so **C** doesn't capture them. Any wagon on a site can load its wheat, and a unit standing on one keeps the enemy's wagons off it.
- **A wagon** on a site, or on one of your cities, gets **Load (L)** and **Unload (U)**. The field beside them takes an amount, and empty moves everything. A wagon can't move after either. A loaded wagon shows a crate: yellow for wheat, brown for bread.
- **Boarding:** with a land unit selected, right-click one of your transports next to it. Selecting the transport's hex lists the units aboard; pick one, and the land it can step onto is tinted.
- **Rations** are listed with each unit. A unit running short gets an amber mark, and a red one when it has none left; the turn's report says which units are starving.

When the game ends, it becomes a replay: you can step through it with both sides in view, and save it as a `.bbr` file.

To play against a bot on disk, let `bitbath serve` serve the page:

```bash
target/release/bitbath serve
```

Then open `http://localhost:8000/web/play.html`. The New game dialog's opponent list now also has the bots on disk, marked "on disk", after the built-in ones.

- **Which bots.** The server offers every Python file in `bots/` and every program in `target/release/`, named by its file without `_bot`. It doesn't check that they are bots, so the list can hold a program that isn't one. Add any other bot with `--bot <name>=<command>`, and pass `--no-discover` to offer only those.

- **The page is still the referee.** The server only starts the bot and carries its lines to the page and back.
- **Nothing from the page.** The server runs only the bots it offers, never a command the page sends. It listens on this machine only, and answers only its own pages.
- **No clock.** A bot is told it has the rules' time for a turn, but the page doesn't hold it to that. A bot that takes over 30 seconds for a turn, or exits, forfeits.
- **The bot's stderr** goes into the replay, turn by turn, as in a game the runner plays.

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

[bots/greedy_bot.py](bots/greedy_bot.py) is a fuller example, including debug annotations that the viewer draws. [bots/smart_bot.py](bots/smart_bot.py) is a stronger heuristic bot that beats it: it focuses fire, masses units before attacking, and picks its builds from the damage table. [bots/vanguard_bot.py](bots/vanguard_bot.py) is stronger again: it plans kills with the fewest attackers, strikes enemies it uncovers while marching, and concentrates on one enemy city at a time. A bot in another language implements the protocol in SPEC §6 directly. The golden test vectors in [tests/vectors](tests/vectors) check hex math and damage.

[bots/solid_bot.py](bots/solid_bot.py) coordinates capturers, scores attacks from reachable hexes, and recruits first at threatened cities or near the front. It caches terrain distances to each city during setup to keep turns quick.

A few sample bots are there for variety. Each plays one style, none of them well enough to worry the bots above:

- [bots/turtle_bot.py](bots/turtle_bot.py) holds its own half of the map: pikemen and archers garrison every city there, and a reserve chases off intruders.
- [bots/volley_bot.py](bots/volley_bot.py) gathers archers into packs that advance together and shoot from two hexes away.
- [bots/raider_bot.py](bots/raider_bot.py) hunts with cavalry, which avoid pikemen and go home to heal.
- [bots/rush_bot.py](bots/rush_bot.py) sends nothing but infantry straight at the enemy and attacks whatever it reaches.

[Bastion](bots/bastion_bot.py) is an independent Python bot. It forecasts neutral-city captures, coordinates attacks, and keeps defenders near threatened cities as its army grows. Run a match against Vanguard with the following command.

```bash
target/release/bitbath play --p1 "python bots/bastion_bot.py" --p2 "python bots/vanguard_bot.py" --map seed:42
```

## Rules and maps

- `--map seed:<n>` generates a point-symmetric map.
- `--map <file>` loads a hand-made map, written as `MAP`, `ROW`, and `CITY` lines. `bitbath map --map seed:<n> --lines` prints one to start from.
- `--config <file>` overrides rules. [configs/default.toml](configs/default.toml) lists the keys. [configs/small.toml](configs/small.toml) is a short game on a small map, and [configs/large.toml](configs/large.toml) is a map four times the default size, for testing how bots and tools cope with scale.
- `--set key=value` overrides a single rule.

## Tournaments

`bitbath tournament` plays every pair of bots on every map, twice with the sides swapped, and then rates the bots:

```bash
target/release/bitbath tournament --bot random="python bots/random_bot.py" --bot greedy="python bots/greedy_bot.py" --bot solid="python bots/solid_bot.py" --config configs/micro.toml --seeds 1-10 --jobs 4
```

- **Maps.** `--seeds` lists the map seeds, as in `1-10` or `3,7,9`. Each `--config` is a set of rules to play them under, and every pair plays under each one, so `--config configs/micro.toml --config configs/small.toml` plays two map sizes.
- **Results.** The results directory, which `--out` names, gets a line per finished game in `games.jsonl`, the ratings in `ratings.json`, and each game's replay and bot directories under `games/`.
- **Resuming and growing.** A game that's already in `games.jsonl` isn't played again. Run the command again with the same `--out` to finish a tournament that was interrupted, or with more bots, seeds, or configs to add only their games.
- **Speed.** `--jobs` sets how many games run at once. Bots are on the clock, so leave each running game two free cores.
- **Ratings.** The ratings are a Bradley–Terry fit on the Elo scale, where a draw counts as half a win. They average 1500, unless `--anchor greedy=1000` pins one bot's rating to hold the scale steady between tournaments. The 95% intervals come from resampling the games, taking a pair's two games on a map together.
- **Diagnostics.** The table also counts each bot's forfeits (crashes, hangs, memory, failed starts), the share of its orders the engine rejected, and its think time per turn.

`bitbath rate <games.jsonl>...` rates the games in one or more results files again, for example with another anchor.

## The benchmark

The benchmark gives a model a starter kit and time to build a bot, then plays the bots that different models built against each other and against the reference bots. It needs Docker and Python 3.10 or later. [benchmark.py](benchmark.py) runs each step:

```bash
python benchmark.py build
python benchmark.py run --harness claude --model <model id> --hours 4
python benchmark.py arena
```

All three steps have been run under Docker, `run` with a script standing in for the model. A real model's session through each of the three harnesses is the part that hasn't been run yet.

### The image

`build` exports the starter kit from `HEAD` and builds one image around it, `bitbath-bench:local`: the kit with the runner built, Python, Rust, g++, and the Codex, Claude Code, and OpenCode command lines at pinned versions. Both later steps use this image, so every bot is built against, and then plays on, the same engine. Build it again after the engine or the kit changes, and start a new season with it.

### Building a bot

`run` starts a container from the image and lets one model work in it, through `--harness claude`, `codex`, `opencode`, or `agy`. It needs the provider's key in the environment, such as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

`agy` is Google's Antigravity CLI. It takes a model slug such as `gemini-4-carbon-high`, where the last word is the reasoning effort, and it needs `GEMINI_API_KEY`: Antigravity's own sign-in is held by the desktop's credential store and can't be copied into a container. A hook that the model can't change limits it to reading and writing files and running commands, which keeps out its web search and browser tools.

- **What the model has.** The kit in `/workspace`, and the kit's README as its task. It can build, play games against the sample bots and its own earlier versions, and read the replays.
- **What it can't reach.** The container's network goes through a proxy that connects only to the model provider's hosts. The harness's web search and fetch tools are off, and none of your own settings, memory, or history are copied in. Other entries and the held-back reference bots are not in the container at all.
- **Time.** `--hours` is the working time, counted over the model's sessions. A model that ends a session early gets another, starting where the files were left, until the time is used. The run also ends once three sessions in a row have each stopped within two minutes without changing the bot. Creating a `FINISHED` file ends the work for good and gives up the time that is left.
- **Sittings.** The time needn't be used in one go. `--at-a-time 2` pauses the entry after two hours of working time, and `python benchmark.py resume <entry id>` goes on with it, with `--at-a-time` again or until the time is used. Between sittings the entry's `/workspace` waits in a Docker volume named `bitbath-<entry id>`, so the model finds its notes, tools and saved versions as it left them, in a new container. Each sitting starts a new session, and the prompt tells the model about the sittings and how much of this one is left. A run that is interrupted, or that fails on a bad key, is paused the same way, and an interrupted session's time still counts. With sittings, three idle sessions end the sitting and not the entry. `resume <entry id> --seal` gives up the time that is left and seals the bot as it stands. `resume` refuses an image that holds another kit. A paused entry isn't sealed, so it plays no tournament, and the volume is removed once the entry is sealed. `python benchmark.py list` shows every sealed and paused entry with its status, the time it has used and has left, and when it was last worked on; `--all` adds the entries that failed. To drop a paused entry, delete its directory and run `docker volume rm bitbath-<entry id>`.
- **Feedback.** Between sessions the benchmark plays the submission against the kit's six sample bots and puts the ratings table in the next session's prompt, with a reminder that the hidden bots are stronger, with how long the last session ran and whether it changed the bot. The model could run those games itself, so this tells it nothing the kit doesn't hold.
- **The check.** What the model leaves in `submission/` is checked with `bitbath check` in a fresh container that has no network. If that fails, the model gets one short session with the check's output to fix it (`--fix-minutes`), and it is checked again.
- **The entry.** A run leaves `entries/<time>-<name>/`: the submission as `submission.tar` and unpacked beside it, `entry.json` with the model, the kit, each session, and the submission's hash, and every session's prompt and output under `rounds/`. An entry that passed its check is sealed. `entries/` isn't committed, since it holds bots that shouldn't be public while a season runs.

The credential has to be inside the container for the harness to work, so use a key made for this, with a spending limit. It is scrubbed from the saved output.

### The arena

`arena` plays every sealed entry and the reference bots in one container that has no network at all.

- **Bots can't see each other.** Each bot's files belong to a user of its own, and its programs run as that user. A bot can't read another bot's code or working directory, the replay being written, or the results. It can't write to its own directory either, and gets an empty working directory each game, so it keeps nothing from one game to the next.
- **What is played.** [bench/arena.json](bench/arena.json) names the reference bots, which are the kit's six sample bots and four that are held back, the rule presets with the seeds to play under each, and the rating anchor. For now every game is on a micro map, as are the practice games between a model's sessions. The seeds there are the held-out ones, so change them before a season that counts if the repository is public.
- **Results.** `arena/<season>/` gets `games.jsonl`, `ratings.json`, and every replay. Run `arena` again to continue a season that was interrupted, or to add new entries: only the missing games are played. A season refuses a bot whose name it knows with different contents, and an image with another kit.
- **Failed checks.** Every bot is built and checked in the arena first. An entry that fails sits the tournament out, and `arena.json` in the season says why.

### The starter kit

`python benchmark.py export <directory>` writes the kit on its own, to read what a model is given.

- **Contents.** The kit holds only what [bench/starter.txt](bench/starter.txt) lists: the engine and runner, the Python kit, six sample bots (random, greedy, turtle, volley, raider, and rush), three rule presets, and SPEC.md. The other bots in `bots/` are left out, so that they can serve as opponents no model has seen. The prompt and the kit's README tell the model roughly how they rate against greedy, so that it knows the scale without seeing their code. Two runs with one of them in the kit both submitted that bot with a number or two changed. [bench/starter/](bench/starter) adds the kit's own README, which is the task a model reads, and a placeholder bot.
- **Source.** The files come from a commit, `HEAD` unless `--rev` names another, and not from the files on disk. Changes that aren't committed don't reach the kit, and `export` warns about them.
- **Identity.** `export` prints the commit and a hash over every file in the kit. The image carries both, and every entry records them.

### Bot directories

A bot directory is the form a bot is submitted in: a directory with a `bot.toml` beside the bot's files.

```toml
run = "python bot.py"
# build = "cargo build --release --offline"
```

- **Running.** A bot directory works wherever a bot command does, as in `--p1 <dir>` or `--bot <name>=<dir>`. The `run` command starts in the game's working directory, like any bot, and whatever it names in the bot directory is found there.
- **Checking.** `bitbath check <dir>` makes sure the directory holds at most 20 MB, runs its `build` command in it, and plays the bot against itself. It fails if either side crashes, hangs, or doesn't start.
- **Building.** Only `check` builds. `play` and `tournament` run a bot directory as it is.
- **Users.** When the runner is root on Linux or macOS, a bot directory's programs run as the user who owns the directory, or, if root owns it, as the user numbered like its group. The runner then also keeps replays and results to itself. This is what the arena's isolation rests on.

## Repository layout

```
engine/         Rust: rules, map generation, vision, order processing. No I/O.
runner/         Rust: the bitbath command (bot processes, clocks, replays, tournaments, ratings).
wasm/           The engine compiled to wasm for the viewer.
web/            The replay viewer and play UI, with its built-in bots.
kits/python/    Python starter kit and its tests.
bots/           Reference bots: random_bot.py, greedy_bot.py, solid_bot.py.
tests/vectors/  Golden test vectors shared by the engine and kits.
configs/        Rule presets.
scripts/        Viewer build and server, and the test vector generator.
bench/          The benchmark: the starter kit, the containers, model runs, the arena.
benchmark.py    The benchmark's command line.
containers/     The benchmark image, and the seccomp profile for Claude Code's sandbox.
```

## Tests

```bash
cargo test --workspace
python kits/python/test_bitbath.py
python -m unittest bench.test_bench
```
