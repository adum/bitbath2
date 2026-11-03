# Bitbath: Design Spec

**Status:** draft, 2026-11-02.

- Decisions from the original brief are treated as settled, as are those listed under [Resolved](#resolved) in §14.
- Everything else is a proposed default. The real uncertainties are listed in [§14 Open questions](#14-open-questions).
- All numbers are starting values, to be tuned by bot-vs-bot tournaments.

## 1. Goals

Bitbath is a turn-based, two-player strategy game on a hex map, played by programs.

1. **Primary: an LLM benchmark.** It measures how well LLMs program strategic bot logic, against each other and against the best human-written bots.
2. **Secondary: fun for humans.** It should be fun to write bots for, to watch, and to play by hand at small scale.

Putting the benchmark first means:

- **Reproducible results.** The engine is fully deterministic: integer math only, no randomness in play, and seeded map generation.
- **Skill decides games, not luck or plumbing.** Combat is deterministic. The framework ships starter kits, helper libraries, and debug tools, so bot authors spend their effort on strategy.
- **Hard to memorize.** It's a new game with configurable rules, and evaluation uses held-out maps and rule variants.

**Not in v1:**

- Sandboxing untrusted code (§10).
- More than two players.
- Exotic mechanics: food logistics, naval units and transport, terrain modification, structures. v1 leaves hooks for them (§12).
- Multi-turn orders in the engine (§5).
- City razing. Cities can only be captured (§14).

## 2. Overview

- **Turns:** players alternate. Player 1 orders all of its units, then player 2 does. One **round** is both turns.
- **Start:** each player has one home city, three units, and some gold. Neutral cities are scattered across the map.
- **Economy:** each player has one shared gold pool. All of their cities pay into it, and it pays for units built at any city they own. Foot units capture enemy and neutral cities.
- **Winning:** **you lose when you own no cities.** If the round limit is reached first, a tiebreak decides.
- **Combat** is deterministic: a damage table between unit types, terrain defense, and defender retaliation.
- **Fog of war:** enemy units are visible only within your vision. Terrain and city locations are known from the start.

## 3. Rules (v1 defaults)

Every value in this section is sent to bots in the init message (§6.2). Any of them can be overridden per match by a config file.

### 3.1 Coordinates and map

The map uses pointy-top hexes with **axial coordinates `(q, r)`**. `r` is the row, starting at 0 at the top and increasing downward. `q` runs along the row. Reference: [Red Blob Games: Hexagonal Grids](https://www.redblobgames.com/grids/hexagons/).

```
row 0:   (0,0)  (1,0)  (2,0)  (3,0)
row 1:      (0,1)  (1,1)  (2,1)  (3,1)
row 2:  (-1,2)  (0,2)  (1,2)  (2,2)
row 3:     (-1,3)  (0,3)  (1,3)  (2,3)
```

**Shape and storage.** The map is a `W × H` rectangle stored row by row. Odd rows are shifted right by half a hex (the "odd-r" layout).

- Column `i` of row `r` holds hex `(q, r)`, where `q = i − floor(r/2)`.
- A hex is on the map if `0 ≤ r < H` and `0 ≤ q + floor(r/2) < W`.

**Directions** are numbered counter-clockwise from east:

| dir | name | Δq | Δr |
|---|---|---|---|
| 0 | E | +1 | 0 |
| 1 | NE | +1 | −1 |
| 2 | NW | 0 | −1 |
| 3 | W | −1 | 0 |
| 4 | SW | −1 | +1 |
| 5 | SE | 0 | +1 |

**Distance** between two hexes is `(|Δq| + |Δr| + |Δq + Δr|) / 2`.

**Size.** The default is 40 × 30. `H` is always even, so the mapping `(i, r) → (W−1−i, H−1−r)` is an exact 180° rotation of the map. Map generation uses this rotation to make maps symmetric (§3.9).

### 3.2 Terrain

| char | terrain | move cost | defense | vision bonus |
|---|---|---|---|---|
| `.` | plains | 1 | 0% | 0 |
| `f` | forest | 2 | 25% | 0 |
| `h` | hills | 2 | 40% | +1 |
| `C` | city | 1 | 30% | 0 |
| `^` | mountains | impassable | – | – |
| `~` | water | impassable | – | – |

- **Defense** reduces the damage taken by a unit standing on that hex.
- **Vision bonus** is added to the vision of a unit standing there.
- City hexes are marked `C` on the map. Who owns each city is part of the game state (§3.6).

### 3.3 Units

All units have 100 max HP.

| code | unit | cost | move | vision | range | flags |
|---|---|---|---|---|---|---|
| `I` | infantry | 100 | 3 | 2 | 1 | capture, zoc |
| `P` | pikeman | 150 | 2 | 2 | 1 | capture, zoc |
| `K` | cavalry | 250 | 5 | 3 | 1 | zoc |
| `A` | archer | 200 | 3 | 2 | 2 only | zoc, no_move_attack |
| `S` | scout | 80 | 6 | 4 | 1 | skirmish |

**Flags:**

- `capture`: can capture cities.
- `zoc`: exerts a zone of control (§3.5).
- `skirmish`: ignores enemy zones of control.
- `no_move_attack`: can't attack on a turn it moved.

**Range.** Each unit type has a range of `range_min..range_max`. The archer's is `2..2`. It can't hit adjacent units, so it can't retaliate against melee attackers.

**Base damage** is the HP a full-HP attacker removes from a defender standing on 0%-defense terrain:

| attacker ↓ / defender → | I | P | K | A | S |
|---|---|---|---|---|---|
| I infantry | 55 | 45 | 40 | 65 | 75 |
| P pikeman | 45 | 50 | 80 | 55 | 70 |
| K cavalry | 70 | 35 | 55 | 85 | 90 |
| A archer | 50 | 55 | 40 | 45 | 70 |
| S scout | 20 | 15 | 10 | 25 | 40 |

**Intended counters:**

- Pikemen beat cavalry.
- Cavalry beats archers and infantry.
- Archers wear down slow foot units from range, but can't move and shoot in the same turn.
- Scouts are for vision, not fighting.

### 3.4 Turn structure

At the start of each player's turn, upkeep runs in this order:

1. **Income.** The player's gold increases by `cities × city_income − units × unit_upkeep`.
   - Gold never goes below 0, and nothing is disbanded.
   - Because of upkeep, the army a player can sustain grows with the number of cities they hold.
2. **Healing.** The player's units standing on the player's own cities heal `city_heal` HP, up to their max.
3. **Capture reset.** Any city whose capturing player no longer has a unit on it loses its capture progress.
4. **Refresh.** The player's units regain full movement points (MP) and their action.
5. **Vision** is computed, and the state message is sent (§6.3).

The player then sends orders. Each order runs the moment it arrives, in arrival order. The turn ends at `END` or at the deadline (§7).

### 3.5 Actions

Each unit has MP and one **action** per turn. An action is either an attack or a capture.

#### Move

A move order gives a path, as a list of directions.

- **Cost:** each step costs the move cost of the hex being entered.
- **Stacking:** the path may pass through hexes holding friendly units. The **final hex must have room**, meaning fewer than `stack_limit` units.
- **Blocked hexes:** a unit can't enter a hex with enemy units, an impassable hex, or a hex off the map.
- **Zone of control (ZOC):** entering a hex next to an enemy unit that has the `zoc` flag sets the mover's MP to 0. Units with `skirmish` are exempt. Leaving such a hex is free.
- **After acting:** a unit can't move after using its action.

**Validation:**

- Before anything moves, the whole path is checked against what the player can currently see.
- The check rejects a path that enters a visible enemy's ZOC on any step but the last.
- If the check fails, the order is rejected and nothing moves.

**Interruptions.** During the move, only hidden information can cut it short:

- **Ambush:** the next hex holds a hidden enemy unit. The mover stops where it is and its MP drops to 0.
- **Hidden ZOC:** the mover enters a hex next to a hidden enemy unit that has the `zoc` flag. The mover stops in that hex.
- **Over the stack limit:** an interrupted move can leave a hex with more than `stack_limit` units. No more units may enter that hex until it's back under the limit.

#### Attack

A unit attacks one enemy unit of its choosing.

- **Target:** a visible enemy unit whose hex is within the attacker's range. Other units on that hex aren't involved.
- **Cost:** attacking uses the action and sets MP to 0.
- **Restriction:** units with `no_move_attack` can't attack after moving.
- **Resolution** follows §3.7.

#### Capture

- **Who:** a unit with the `capture` flag, standing on a city its player doesn't own. It may have moved onto the city earlier in the same turn.
- **Effect:** the unit uses its action to add its current HP to the city's capture progress.
- **Completion:** when the progress reaches `capture_points`, the city changes owner and the progress resets.
- **Duration:** at full HP, a capture takes two turns.

#### Build

- **Where:** at a city you own that has no enemy units on it and fewer than `stack_limit` of your units.
- **Cost:** you pay the unit's cost, and the new unit appears on the city hex.
- **Limit:** one build per city per turn.
- **New units** can't move or act until your next turn.

### 3.6 Cities

- **Visibility:** city locations are part of the map, and **who owns each city is public.** Capture progress is shown only for cities you can currently see.
- **Vision:** each city gives its owner vision within a radius of `city_vision`.
- **Neutral cities** (owner 0) start empty and earn nothing. They are captured the same way as enemy cities.

### 3.7 Combat

```
damage(att, def) = base[att.type][def.type] * att.hp * (100 - defense(def.hex))
                   / (att.max_hp * 100)
                   -- integer division; every operand is non-negative
                   -- at least 1 if base > 0
```

1. **Target.** The attacker picks which enemy unit to attack, and that unit is the defender. Stacking gives no protection beyond terrain: any unit in a stack can be targeted directly.
2. **Damage.** The defender takes the damage. At 0 HP or below, it is removed.
3. **Retaliation.** If the defender survives, it strikes back using its new HP and the attacker's terrain defense. This happens only when both of these hold:
   - The attacker's distance is within the defender's range.
   - The defender's base damage against the attacker's type is above 0.
4. **Other rules:**
   - The attacker doesn't advance into the hex.
   - A unit can retaliate any number of times during an enemy turn.

**Worked example.** A full-HP infantry unit on plains attacks an infantry unit on hills (40% defense).

- The defender takes 55 × 100 × 60 / 10000 = **33** damage and has 67 HP left.
- It retaliates for 55 × 67 × 100 / 10000 = **36**, leaving the attacker with 64 HP.

### 3.8 Winning

- **Out of cities:** a player who owns no cities loses immediately. This is checked after every order.
- **Round limit:** after `round_limit` rounds, the tiebreak goes in this order:
  1. More cities.
  2. Higher army value (Σ `cost × hp / max_hp`).
  3. Otherwise a draw.
- **Forfeit:** a bot that crashes, exceeds its memory limit, or hangs forfeits (§7).

### 3.9 Map generation

- **Seeded and point-symmetric:** the bottom half is the top half rotated 180° (§3.1). The two home cities are mirror images of each other, and so are the neutral cities.
- **Terrain** comes from seeded noise. Clusters of mountains and water create chokepoints.
- **Validation:** the generator rerolls (deterministically) until every city can be reached from both home cities.
- **Starting setup:** each player gets one home city with `I, I, S` on it, plus `start_gold`.
- **Random number generator:** the generator uses its own SplitMix64 generator. It avoids `rand::StdRng`, which isn't guaranteed to give the same output across versions.
- **Map identity:** a map is identified by its generator version, seed, `W`, `H`, and generation parameters. Any change to the generator bumps its version.
- **Hand-made maps** can be loaded from files that use the same `MAP`, `ROW`, and `CITY` lines as the init message.

### 3.10 Rule constants

| key | default | meaning |
|---|---|---|
| `map_width`, `map_height` | 40, 30 | `map_height` must be even |
| `neutral_cities` | 10 | must be even (mirrored pairs) |
| `stack_limit` | 4 | max units per hex |
| `round_limit` | 200 | rounds before the tiebreak |
| `start_gold` | 300 | |
| `city_income` | 100 | gold per owned city per turn |
| `unit_upkeep` | 5 | gold per unit per turn |
| `city_heal` | 20 | HP healed per turn on your own city |
| `city_vision` | 2 | |
| `capture_points` | 200 | capture progress needed to take a city |

Time constants are in §7.

## 4. Fog of war

- **Visible hexes:** a hex is visible to you if either of these is true:
  - It's within one of your units' vision. A unit's vision is its unit vision plus the vision bonus of the hex it stands on.
  - It's within `city_vision` of one of your cities.

  There's no line-of-sight blocking in v1.
- **What you see:** all of your own units, plus enemy units on visible hexes.
- **Always known:** terrain and city locations. This is fog, not shroud (shroud would hide unexplored terrain).
- **During your turn, vision only grows.** Enemies don't move during your turn, so an enemy unit you've seen stays visible until `END`. Newly revealed units arrive as `SEE` events (§6.4).
- **Shroud** could be added later. Even with shroud, a bot on a symmetric map can work out where the enemy's home city is.

## 5. Players and modes

The engine talks to two *players* over the protocol in §6. All three modes use that same protocol. They differ only in what's on the other end.

| mode | the player is | milestone |
|---|---|---|
| 2. Programmed | a bot process | v1 |
| 1. Manual | a browser UI for humans, or an LLM harness that turns the state into a prompt and the LLM's tool calls into orders | M5 |
| 3. Combo | a supervisor that runs a bot process and can pause between turns to consult an LLM | M5 |

**Manual play needs small maps.** A human can't direct hundreds of units by hand, and an LLM reading a large state every turn gets expensive. The project should ship small-map configs for this mode.

**Conveniences belong outside the engine.** "Move to x,y", standing orders, and auto-explore go in the kit library, the UI, or the LLM harness. This keeps engine orders atomic and the rules unambiguous.

**Mode 3 sketch:**

- **When it pauses:** at checkpoints every K rounds, and whenever the bot sends a `PAUSE` request.
- **What the LLM gets:** the current state, the replay so far, and the bot's logs.
- **What the LLM can do:** send the bot a strategy line, replace the bot's code, or let it continue. A replaced bot restarts, but its working directory persists, so it can keep memory in files.
- **Pause budget:** pauses come from a fixed budget per game. Without a budget, mode 3 turns into mode 1.

## 6. Protocol

### 6.1 Conventions

- **Format:** text over stdin/stdout, one message per line, ASCII.
- **Tokens** are separated by whitespace, with the keyword first. All numbers are integers.
- **Line endings:** each line ends with `\n`. A trailing `\r` is ignored.
- **IDs:** unit IDs are assigned in creation order and never reused. City IDs run from 0 to N−1.
- **Notation:** below, `>` marks engine → bot and `<` marks bot → engine.
- **Flush stdout after every line.**
  - When Python's stdout is piped, it's fully buffered. Unflushed orders sit in the buffer and are lost at the deadline.
  - The kits handle flushing.
  - The Python kit also sends `print` output to stderr, so stray prints can't corrupt the protocol.
- **stderr** is free-form logging. It's captured into the replay, capped per turn.
- **Full state, not diffs:** the static setup is sent once. After that, every turn message carries the full current state.

### 6.2 Init (once per game)

```
> BITBATH 1                              protocol version
> PLAYER 1                               your id (1 or 2); neutral is 0
> MAP 40 30                              width height
> ROW ....ff..hh....~~~...               × height, row 0 first (§3.1); abbreviated here
> CITY 0 3 2 1                           id q r initial_owner, one per city
> TERRAIN . plains 1 0 0                 char name move_cost defense_pct vision_bonus (move_cost 0 = impassable)
> UNITTYPE I infantry 100 100 3 2 1 1 capture,zoc
>                                        code name cost max_hp move vision range_min range_max flags ('-' = none)
> DAMAGE I 55 45 40 65 75                attacker I's base damage vs each type, in UNITTYPE order
> RULE stack_limit 4                     one line per constant in §3.10 and §7
> INIT
< READY                                  within init_ms
```

Both bots initialize at the same time.

### 6.3 Turn state (start of each of your turns)

```
> TURN 12                                round number
> TIME 5500 4500                         ms available this turn, ms left in your bank
> GOLD 450
> CITY 0 1 0 0                           id owner capturer progress (every city; capturer and progress are 0 unless visible)
> UNIT 17 1 I -2 5 30                    id owner type q r hp (your units, then visible enemy units)
> TILE 5 7 f                             q r terrain: each tile that differs from the initial map (none in v1)
> LOG HIT 91 K -1 5 17 30 88             attacker id type q r, your unit id, hp after for your unit and the attacker
> LOG CITY 3 1 2                         city id, old owner, new owner
> GO
```

**Opponent log.** `LOG` lines report what happened during the opponent's most recent turn, in the order it happened:

- `LOG HIT`: an enemy unit attacked one of your units. The line gives the attacker's ID, type, and position at the time, your unit's ID, and both units' HP after the attack and any retaliation. It reveals the attacker even if the attacker is now out of sight.
- `LOG CITY`: a city changed hands.

Enemy movement isn't logged, because the state already shows where visible enemies are. Bots should skip `LOG` kinds they don't recognize, so new kinds can be added later without breaking them.

### 6.4 Orders and replies

```
< M <unit> <dirs>          move along a path; dirs is a string of direction digits, e.g. 0012
< A <unit> <target>        attack the enemy unit whose id is <target>
< C <unit>                 capture the city the unit stands on
< B <city> <type>          build a unit at a city
< D ...                    debug annotation (§8): no reply, no effect on the game
< END <round>              end of turn
```

**Replies:**

- Every `M`, `A`, `C`, and `B` order gets exactly one **result** line. Zero or more **event** lines may come before it.
- To read a reply, read lines until one starts with `OK`, `PART`, or `ERR`.
- Bots may send many orders before reading any replies. Replies always come back in order.

```
> SEE <id> <owner> <type> <q> <r> <hp>          event: an enemy unit became visible
> OK <q> <r> <mp_left>                          move completed
> PART <q> <r> ambush|zoc                       move cut short by a hidden enemy
> OK <target_hp> <attacker_hp>                  attack resolved (hp 0 = dead)
> OK <progress> <capture_points>                capture in progress
> OK captured                                   the city changed hands
> OK <unit_id>                                  unit built
> ERR <code> <explanation>                      rejected; nothing changed
```

**Error codes:**

| group | codes |
|---|---|
| order format | `syntax`, `unknown`, `not_yours` |
| unit state | `acted`, `no_mp`, `new_unit` |
| movement | `path`, `zoc`, `stack_full` |
| attack and capture | `range`, `no_target`, `cannot_capture` |
| building | `no_gold`, `city_used` |
| limits | `limit`, `late` |

The explanation after the code is plain English, for humans and LLMs. Invalid orders never break the game.

**Game end:** the engine sends `GAMEOVER <winner> <reason>` to both bots. A winner of 0 means a draw. Bots should exit on `GAMEOVER` or end of input.

### 6.5 Example

This is player 1's first turn. Player 2's starting units are IDs 4–6, and the hex at (3,3) is a mountain. Gold is 300 to start, plus 100 income, minus 15 upkeep, which gives 385.

```
> TURN 1
> TIME 6000 5000
> GOLD 385
> CITY 0 1 0 0
> CITY 1 2 0 0
> CITY 2 0 0 0
> ...
> UNIT 1 1 I 3 2 100
> UNIT 2 1 I 3 2 100
> UNIT 3 1 S 3 2 100
> GO
< M 3 000
> OK 6 2 3
< B 0 P
> OK 7
< M 1 5
> ERR path (3,3) is mountains
< D HEX 9 2 yellow scout target
< END 1
```

## 7. Time control

The clock is Fischer-style with partial banking.

- **Available time:** each turn you have your bank plus `time_increment_ms`.
- **Under the increment:** `time_keep_pct`% of the unused part goes into the bank, up to `time_bank_cap_ms`.
- **Over the increment:** the excess comes out of the bank.
- **What's measured:** the clock runs from when the engine sends `GO` until it receives `END`. In v1 this is wall-clock time (see §11).

| key | default |
|---|---|
| `time_bank_ms` (initial) | 5000 |
| `time_increment_ms` | 1000 |
| `time_keep_pct` | 50 |
| `time_bank_cap_ms` | 10000 |
| `init_ms` | 10000 |
| `hang_turns` | 3 |
| `max_orders_per_turn` | 20000 |
| `max_line_bytes` | 1024 |

**Missed deadlines:**

- **Deadline:** orders that arrive after the deadline get `ERR late` and aren't applied. The turn is over, but the bot is **not** killed.
- **Resync:** the engine sends your next `TURN` only after it has received `END <round>` for your previous turn. If that `END` hasn't arrived when your next turn starts, the waiting time counts against the new turn.
- **Hang:** a bot that sends no `END` for `hang_turns` turns in a row forfeits.
- **Kill:** the engine kills a bot only if it crashes, exceeds its memory cap, or hangs. Killing a bot for a single timeout would wipe its memory for the rest of the game.
- **Pondering** is allowed in v1: a bot may keep computing during the opponent's turn. Tournaments should give each bot its own CPU core (§11).

## 8. Debugging and replays

**Debug annotations** are recorded in the replay and drawn by the viewer, up to a cap per turn:

- `D HEX <q> <r> <color> [label]`
- `D LINE <q1> <r1> <q2> <r2> <color>`
- `D TEXT <text>`

Colors are `red`, `green`, `blue`, `yellow`, `orange`, `purple`, `white`, `black`, or `#rrggbb`.

**stderr** is captured each turn, up to a cap.

**Replay files** (`.bbr`) are JSON Lines:

- **Header:** engine and protocol versions, the full rules, the map, the seed, and player names and commands.
- **One line per turn:**
  - the round, the player, and the time used;
  - every order line received before the deadline, including rejected ones;
  - late lines;
  - debug annotations;
  - stderr.
- **Final line:** the result, plus stats for each player (invalid orders, late orders, timeouts, think time).

**Re-simulation.** Replaying a game doesn't re-run the bots. The deterministic engine re-simulates the recorded orders, which keeps files small and verifiable. The web viewer does the same, using the engine compiled to wasm.

**Text summary.** `bitbath replay <file> --text` prints a turn-by-turn summary for humans and LLMs to read.

## 9. Support for bot authors

**Starter kits.** Python comes first, then one compiled language. Each kit provides:

- protocol I/O, with flushing
- a parsed state object
- hex helpers: neighbors, distance, line, ring, storage↔axial conversion, mirror
- a pathfinder that outputs `M` direction strings
- a combat predictor that matches the engine exactly
- an example bot

Bots in languages without a kit implement §6 directly. The protocol is small, and the golden test vectors check the tricky parts.

**Golden test vectors.** Files in `tests/vectors/*.json` cover hex math, damage, retaliation, and scripted order sequences with their expected replies. The engine's tests and every kit's tests run them, so no library can quietly disagree with the engine.

**Reference bots** form a graded ladder:

- `random`: plays legal moves only.
- `greedy`: expands to the nearest city and attacks whatever is adjacent.
- `solid`: a decent heuristic.

They serve as starter examples, as benchmark rungs, and as the harness for balance testing.

**Docs for bot authors.** Once v1 stabilizes, §3–§8 move into `docs/RULES.md` and `docs/PROTOCOL.md`. They're written to be the only thing a bot author, human or LLM, needs. Any ambiguity in them turns the benchmark into a guessing test.

## 10. Runner and security

```
bitbath play --p1 "python bots/greedy.py" --p2 "./mybot.exe" --map seed:42 --config default.toml --replay out.bbr
```

**Trusted code in v1.** Code written by LLMs tends to be careless rather than malicious, so each bot still gets these protections:

- its own working directory
- a memory cap
- its whole process tree killed when it exits. This uses a Job Object on Windows and a process group on Unix.

**I/O threads.** Each bot has its own reader and writer threads with queues. A bot that never reads its replies can't deadlock the engine.

**Launcher interface.** Bots are started through a `BotLauncher` interface, so other ways of running them can be added later:

- **WASI with wasmtime fuel metering.** This gives a deterministic compute budget and a real sandbox, so games become reproducible on any machine. The cost is language coverage: plain Python works, but native extensions like numpy mostly don't.
- **Containers.**

## 11. Benchmark design

- **Pairs:** every matchup is a pair of games on the same map, with the players swapping sides.
- **Maps and rule variants:**
  - **Development seeds** are public.
  - **Evaluation seeds** are held out.
  - **Rule variants** are held out too. They change costs and damage, or remove unit types. Bots have to read the rules message, so the benchmark measures strategy instead of a memorized set of tactics.
- **Opponents:** LLM bots play the reference ladder, each other, and the best human bots.
- **Ratings:** a Bradley–Terry (or Elo) fit over all games, with bootstrap confidence intervals.
- **Diagnostics:** invalid-order rate, late orders, timeouts, crashes, and think time. They separate misread rules and plumbing failures from bad strategy.
- **Reproducibility:**
  - In v1, wall-clock timing makes results depend somewhat on hardware and machine load. Record both with each run, and give each bot its own CPU core, since bots may think during the opponent's turn.
  - Every game is still exactly replayable from its file.
  - Full reproducibility arrives with the fuel-metered WASI launcher (§10). Battlecode's bytecode limits are the precedent for this.
- **LLM development protocol:** this goes in a separate `BENCHMARK.md`, still to be written. It covers:
  - which docs the model gets
  - the toolchains available to it. Any language is allowed (§14), so this list defines what "any" means in practice.
  - access to the local runner and opponents
  - the number of iterations and the token budget
  - access to replays and logs
- **Prior art:** [CodeClash](https://github.com/codeclash-ai/CodeClash) runs multi-round tournaments in which LLMs edit their code between competitions, in existing arenas such as Halite and BattleSnake. Bitbath differs in two ways:
  - It's a new game, so no strategy write-ups are in training data.
  - It has rule variants.

## 12. Architecture

```
bitbath2/
  engine/          Rust: rules, state, map generation, vision, replay re-simulation. Pure: no I/O, clock, or threads.
  runner/          Rust binary `bitbath`: bot processes, protocol I/O, clocks, replays, tournaments.
  wasm/            wasm-bindgen wrapper around engine for the browser.
  web/             TypeScript + Canvas replay viewer; later the human play UI.
  kits/python/     Python starter kit.
  bots/            reference bots.
  tests/vectors/   golden test vectors shared by the engine and kits.
  docs/            RULES.md and PROTOCOL.md for bot authors (later).
```

**Why Rust:** wasm matters more than raw speed. Bot think time, not the engine, decides how long a game takes. The same engine crate builds both the native runner and the browser viewer and play UI, so there's never a second copy of the rules to drift out of sync.

**Determinism rules for `engine`:**

- **Integers only:** no floating point anywhere in game logic.
- **Non-negative division only:** Python's `//` rounds down, while Rust's `/` rounds toward zero. Negative operands would make the kits disagree with the engine.
- **No dependence on hash order:** never iterate a `HashMap` or `HashSet`. Use `Vec`, `BTreeMap`, or sorted keys.
- **No clock and no other randomness:** the only randomness is the seeded map generator.
- **Pure order processing:** processing an order is a pure function, `(state, player, order line) → (new state, reply lines)`.

**Hooks for later mechanics:**

- **Terrain modification:** the turn state already carries `TILE` changes.
- **Transport:** a unit's location can be a hex *or* a carrier unit. Reserve an optional trailing field on `UNIT` for this.
- **Logistics:** food, supply, and starvation become new steps in the upkeep list (§3.4).
- **Structures:** model them as immobile unit types (move 0), adding new flags as needed.

## 13. Roadmap

1. **M1, Engine:**
   - hex math, map generation, state, orders, combat, vision, upkeep, victory
   - unit tests and golden test vectors
2. **M2, Runner:**
   - bot processes, the clock, late orders and resync, replay files
   - the Python kit plus the `random` and `greedy` bots
   - bot-vs-bot games from the CLI
3. **M3, Viewer:** wasm re-simulation in the browser, with debug annotations.
4. **M4, Balance and benchmark:**
   - the `solid` bot, the tournament runner, and ratings
   - a first balance pass on damage and costs
   - benchmark time controls, chosen from measured bot think times (§14)
5. **M5, Other modes:** human play in the browser, the LLM harness (mode 1), and the mode 3 supervisor.
6. **Later:**
   - the WASI/fuel launcher and more language kits
   - shroud, and suspending the idle bot
   - exotic mechanics

## 14. Open questions

1. **Benchmark time controls:** at 1 s per turn, a 200-round game can take about 7 minutes. Should benchmark runs use less? *Deferred to M4, to be decided from measured bot think times.*

### Resolved

- **Economy model** (2026-11-02): one shared gold pool per player, not per-city production.
- **Army size** (2026-11-02): limited by unit upkeep, not a hard unit cap.
- **Combat against stacks** (2026-11-02): the attacker picks the target unit. Stacking gives no protection beyond terrain.
- **Zones of control** (2026-11-02): on by default. Entering a hex next to an enemy unit with `zoc` ends the mover's movement; `skirmish` units (scouts) are exempt.
- **City razing** (2026-11-02): capture only in v1. Razing may come later as a rule option; a razed hex would become plains, reported through `TILE` lines.
- **Crashes** (2026-11-02): a bot that crashes forfeits immediately, the same as one that exceeds its memory cap or hangs.
- **Opponent log** (2026-11-02): each turn state includes a short `LOG` of the opponent's last turn, covering attacks on your units and cities that changed hands (§6.3).
- **Pondering** (2026-11-02): allowed in v1, so bots may compute during the opponent's turn, and tournaments give each bot its own CPU core. Revisit with the WASI launcher, where the engine controls exactly when a bot runs.
- **Language policy** (2026-11-02): LLM bots may use any language on the leaderboard, so choosing a language is part of the skill being measured. `BENCHMARK.md` will list the toolchains the benchmark environment provides.
- **Default scale** (2026-11-02): 40 × 30 maps, with roughly 100–200 units per side at most. Small presets for manual play and large presets as rule variants come later. Revisit after the M4 balance pass if games feel cramped.
