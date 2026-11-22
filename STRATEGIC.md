# Bitbath: Strategic Ruleset

**Status:** design draft, 2026-11-22. Phase 1 (§12) is being built: the engine has the supply rules, but map generation, kits, and bots don't support them yet.

- The choices agreed so far are listed in [§14](#14-agreed-so-far). Everything else is a proposal.
- All numbers are starting values, to be tuned by tournaments as the default rules were.
- The questions that still need a decision are in [§13](#13-open-questions).
- "SPEC §n" means a section of [SPEC.md](SPEC.md).

## 1. Goals

The default game is tactical: a 40 × 30 map, short distances, and one shared pool of gold. The strategic ruleset is a second game on the same engine, about territory and supply:

- **A big, open map** with long distances, where holding ground and keeping it supplied matter more than any single fight.
- **Resource gathering:** wheat grows at a few sites and has to be carried to a city, where it becomes bread. Bread builds units and feeds them.
- **Sieges:** a city that can't be supplied weakens and falls. A city with its own supply is a fortress.
- **Ships:** transports, warships, and islands worth invading.
- **Long wars:** a game is meant to last hundreds or thousands of rounds, and ends when one side runs out of cities.

The default game stays as it is.

**A ruleset, not a mode.** SPEC §5 uses *mode* for who is playing. The strategic game is a *ruleset*: a config file that turns on the rules below. It's played with the same engine, protocol, runner, viewer, and modes as the default game.

**Principles:**

- **Supply is visible.** Goods travel in units on the map, which can be seen, chased, and escorted. Nothing arrives by an invisible route (§14).
- **Cities are the hubs.** Wheat becomes bread only in a city, and units eat only bread, so every unit built and every meal eaten passes through a city.
- **Few, big carriers.** Sites store many turns of output, and one wagon carries many turns' worth. Keeping an empire supplied should take a handful of orders a turn.
- **Same machinery.** Combat, zones of control, capture, fog of war, fortification, and the determinism rules (SPEC §12) carry over. New mechanics are data where possible: terrain columns, unit flags, and rule constants.
- **Readable from the rules message.** Everything a bot needs still comes in the init message (SPEC §6.2), so rule variants keep working.

## 2. Overview

- **Sites** grow wheat every turn and pile it up, to a cap. Each site has its own output and cap. They're captured like cities.
- **Cities** turn wheat into bread the moment it arrives, and make a little bread of their own. Each city keeps its own bread, which pays for the units built there and feeds the units on and around it. There's no gold.
- **Wagons** carry wheat from sites to cities, and bread from cities to armies.
- **Every unit eats** a ration a turn when it holds still, and more when it moves or attacks. It carries 30, and refills them with bread from a city or wagon on or next to its hex. A unit with none left starves.
- **Sieges** follow from these rules. A city under siege makes no bread. If no wagon gets in, it stops building once its stockpile is gone, and its garrison starves once their rations run out. A city with a site next to it is supplied every turn, with no wagon.
- **Ships** sail on water, which land units can't enter. Transports carry land units, wagons included. Warships fight at sea, and sinking a transport sinks everything aboard.

## 3. Map

- **Size:** 80 × 60 with 48 cities, the large preset, to start. Larger maps later.
- **Water:** about 40% of the map instead of about 6%, which makes seas, coasts, and islands. Maps stay point-symmetric (SPEC §3.9).
- **Resource sites** are a new terrain, `R`: move cost 1, with no defense, vision bonus, or concealment. The generator places `sites` of them in mirrored pairs:
  - one next to each home city, owned by that player from the start;
  - half of the other pairs, rounded down, next to neutral cities, at most one per city, which makes those cities fortresses worth fighting over;
  - the rest in the open, at least 3 hexes from any city, where only wagons can collect them. Some of these are on islands.
- **Each site has its own output and cap,** which the generator picks from the ranges in §10. The two sites of a mirrored pair match.
  - The sites next to the homes get the middle of each range. Sites get richer the farther they are from the nearer home, so the best ones are in the middle of the map and on islands. A neutral city next to a rich site is the biggest prize on the map.
  - Output and cap are picked separately, so sites come in kinds. A *fast* site, with high output and a small cap, fills up quickly, so it wants a city next to it or frequent visits. A *deep* site, with low output and a big cap, can be left alone for 50 turns and emptied in one haul, which also makes it a tempting raid.
- **Validation:** instead of every city being reachable by land (SPEC §3.9), every city and site must be reachable from both homes by land and sea, and each home's landmass must have a port (§6).
- **Start:** each player gets `I, I, S` and a wagon on their home city, and `start_bread` in it.
- **The default game's maps don't change.** The new generator parameters default to today's behavior.

## 4. Wheat and bread

There are two kinds of goods. **Wheat** grows at sites. **Bread** is made from it in cities: wheat becomes bread, one for one, the moment it enters a city. Bread builds units and feeds them, and nothing eats wheat, so an army can't live off a site. It needs a city behind it.

### Sites

- **Production:** at the start of its owner's turn, a site adds its output of wheat to its pile, up to its cap. Neutral sites produce nothing.
- **Occupied sites:** a site neither produces nor delivers while an enemy unit stands on it, so a raider can cut off a site without capturing it.
- **Capture:** sites are captured like cities (SPEC §3.5), with the `C` order and `capture_points`. The pile goes with the site, so taking a site its owner has left alone for a while takes everything on it.
- **Vision:** a site gives its owner vision like a city (`city_vision`).
- **Next to a city:** right after production, a site next to one of its owner's cities moves its whole pile into that city, where it becomes bread. If there are several such cities, the lowest city id gets it. This is what it means for a city to have its own supply.

### Cities

- **Stockpile:** each city stores any amount of bread.
- **Making bread:** at the start of its owner's turn, a city makes `city_bread` bread, unless an enemy unit stands on it or next to it. Neutral cities make none.
  - This keeps a player from ever being stuck. One with no sites and no wagons can still build, slowly: on its own, a city builds a scout in 8 turns, or infantry or a wagon in 10.
  - It doesn't help a city under siege, which has to live on its stockpile, a site next to it, and the wagons that get through. Without that exception, 10 bread a turn would keep two units holding still alive in every besieged city forever.
  - A single enemy unit next to a city, even a scout, stops its bread. That's cheap harassment, but it costs the city only `city_bread` a turn, and the unit is easy to kill.
- **Building** pays the unit's cost in bread from that city's stockpile. Otherwise it works as in SPEC §3.5. To build near the front, send bread there first.
- **Capture:** whoever captures a city takes its stockpile.
- **No gold:** there's no shared pool, gold income, or gold upkeep. Neutral cities start empty.

### Wagons

- **Capacity:** a wagon carries up to `cargo_max` goods, wheat and bread together.
- **Loading:** `L <wagon>` takes wheat from the site the wagon stands on, or bread from the city it stands on, as much as fits. Its player must hold the site or city.
- **Unloading:** `U <wagon>` puts the wagon's goods into the city it stands on, which its player must hold. The wheat becomes bread. A wagon can also carry bread from one city to another.
- **Amounts:** both orders take an optional amount, to move at most that much, for instance to leave some bread in a city. Unloading takes wheat first.
- **One stop:** a wagon can load and unload as often as it likes in a turn, but can't move after doing either. So a wagon can drive into a city, unload its wheat, and take bread on, all in one turn.
  - A wagon passing through a city delivers nothing.
  - Even a site next to a city would take two turns per round trip, which is why such sites deliver by themselves.
- **Losing a wagon loses its cargo.** The goods aren't dropped or captured.
- **Feeding:** wagons also feed the units on and next to them with their bread (§5). Wheat feeds no one.

## 5. Food and sieges

**Rations.** Every unit carries up to `ration_max` rations. New units start full.

**Eating.** Each turn costs a unit `eat_base` rations, plus `eat_move` if it moved and `eat_attack` if it attacked. With the starting values, holding still costs 1, moving or attacking 2, and both 3.

- Moving means changing hexes, as it does for fortification (SPEC §6.6), so boarding and landing count. Riding aboard a moving transport doesn't.
- Only attacks on the unit's own turn count. Striking back costs nothing extra, and neither does capturing.
- A full unit can hold still for 30 turns, march for 15, or march and fight for 10. Fifteen turns of marching take infantry roughly halfway across the map.
- Holding still is cheap, which suits defense: a unit holding a city digs in and eats the least. Attacking costs the most, so an offensive needs supply.

**Upkeep** at the start of each turn runs in this order, in place of income (SPEC §3.4):

1. **Production:** sites grow wheat, sites next to cities deliver it, and cities make bread (§4).
2. **Feeding:** each of the player's units eats what its last turn cost, then refills to `ration_max` from its player's cities and wagons on its hex or next to it, paying `ration_cost` bread per ration.
   - Units are served in id order.
   - Each takes from its sources in turn, cities before wagons and lower ids first, until it's full or they run out. A source pays for whole rations only.
   - A unit that can't cover what it ate, from its rations and a refill together, is left with none and starves: it loses `starve_hp` HP, and can die of it. So a unit that reaches bread in time never starves.
3. **Healing,** as in SPEC §3.4, except that a unit that starved this turn doesn't heal. Ships heal next to their own cities (§6).
4. **Capture reset, refresh, and vision,** as in SPEC §3.4. Capture reset covers sites too.

**What it costs.** Every ration is paid for in bread when the unit refills, at `ration_cost` each. With the starting values, a unit holding still costs 5 bread a turn, the same as gold upkeep in the default game. One that moves costs 10, and one that moves and attacks costs 15. So bread limits the army a player can keep, and an offensive costs more than holding a line. If cavalry raids turn out too cheap, a per-type appetite, such as cavalry eating double, is the obvious next knob.

**Sieges** come out of these rules. With the starting values:

- Say a city holds 300 bread and 4 units, and the besiegers stand next to it, so it makes no bread. Holding still, the garrison eats 4 rations a turn, which costs 20 bread. If no wagon gets in, the stockpile lasts 15 turns.
- Then the garrison lives on its rations for 30 turns, and then starves, at 10 HP a turn. Starving units also hit weaker, since damage scales with HP.
- A city with a site next to it takes in that site's wheat every turn, as bread. A site that grows 50 a turn feeds 10 units holding still. To starve such a city, the besieger has to stand on the site or capture it.
- Besiegers eat too. Holding still, they last 30 turns on their rations, and starving a city out takes 45 turns or more, so a siege that waits needs wagons behind the lines, which a relief force can raid.
- **Starving a city out is slow,** which suits games meant to last hundreds or thousands of rounds. If sieges drag even so, `ration_max` is the knob.

**In the field,** an army needs wagons of bread behind it or stocked cities along the way, so how far it can go depends on its supply. A captured city comes with its stockpile and starts making bread, so taking cities along the way helps feed an advance.

## 6. Ships

### Water and ports

- **Naval move cost:** terrain gets a second move cost, for ships. Water's is 1, and every other terrain's is 0, which means ships can't enter it. Land units still can't enter water.
- **Naval units** have the `naval` flag and move by the naval move cost. Otherwise ships move as land units do: paths, stacking, zones of control, and ambushes all work the same (SPEC §3.5).
- **Ports:** a city next to water is a port. Ships don't enter cities.
  - A port builds a ship onto the first water hex next to it, in direction order (SPEC §3.1), that has room and no enemy units.
  - A ship next to one of its player's cities is in port. It heals there by `city_heal` and refills its rations from that city (§5).

### Transports

- **Capacity:** a transport carries up to `passengers_max` land units, wagons included.
- **Boarding:** a land unit boards by moving onto the transport. As its last step, a move may enter a water hex that holds one of its player's transports with room. Boarding ends the unit's move.
- **Aboard,** a unit moves with the transport.
  - It can't attack, capture, load, or unload, and it can't be attacked. Only the transport can.
  - It doesn't count toward the hex's stack limit.
  - It still sees from the transport's hex, and still eats. A wagon aboard feeds the units around it, like any wagon.
- **Landing:** a unit aboard gets off with a one-step move onto land next to the transport, which can be a city.
  - Landing ends the unit's move but leaves its action, so it can attack or capture where it lands, as after a move that ends in a zone of control.
  - Passengers can land after their transport moves in the same turn, so a transport can sail and unload in one turn.
  - A unit can't board and land in the same turn.
- **Sinking:** when a transport is destroyed, everything aboard is destroyed with it, cargo included.
- **Goods at sea** always ride in wagons. Transports don't hold goods themselves (§14).

### Warships

- **The galley** (`G`, §7) is the one warship type to start with. It fights other ships, and can bombard land units on the coast next to it.
- **Zones of control** reach across the shoreline both ways. A line of galleys can close a strait, and pikemen on a beach slow a landing.
- **Blockades** need no rule of their own. Galleys off a port sink the transports that come out, and their zones of control stop transports from slipping past.
- **Fleets eat too.** A ship that sails every turn runs out after 15 turns away from port, so a longer voyage needs a wagon of bread aboard a transport nearby.
- **From land,** only archers can hurt ships (§7).

## 7. Unit types

The five land units of the default game are unchanged (SPEC §3.3), except that their costs are paid in bread. The ruleset adds three:

| code | unit | cost | move | vision | range | flags |
|---|---|---|---|---|---|---|
| `W` | wagon | 100 | 4 | 2 | – | carry |
| `T` | transport | 150 | 6 | 2 | – | naval, transport |
| `G` | galley | 200 | 5 | 3 | 1 | naval, zoc |

**New flags:**

- `carry`: holds goods, wheat and bread, and can load and unload them (§4).
- `naval`: moves on water, by each terrain's naval move cost (§6).
- `transport`: carries land units (§6).

**Units that can't attack.** Wagons and transports can't attack or strike back. Their `UNITTYPE` lines give range 1, since every unit type needs a range, but their damage rows are all 0. An attack whose base damage is 0 is rejected. This is a new rule for the engine. It changes nothing in the default game, where every base damage is above 0.

**Base damage**, with starting guesses for the new pairs:

| attacker ↓ / defender → | I | P | K | A | S | W | T | G |
|---|---|---|---|---|---|---|---|---|
| I infantry | 55 | 45 | 40 | 65 | 75 | 70 | 0 | 0 |
| P pikeman | 45 | 50 | 80 | 55 | 70 | 60 | 0 | 0 |
| K cavalry | 70 | 35 | 55 | 85 | 90 | 90 | 0 | 0 |
| A archer | 50 | 55 | 40 | 45 | 70 | 70 | 40 | 40 |
| S scout | 20 | 15 | 10 | 25 | 40 | 40 | 0 | 0 |
| W wagon | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| T transport | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| G galley | 25 | 25 | 25 | 30 | 35 | 50 | 80 | 55 |

- **Wagons** are easy prey, for cavalry most of all. That gives cavalry a strategic job: raiding supply. Killing a wagon of wheat cuts its owner's income, and killing one of bread starves an army.
- **Transports and galleys** can be hurt only by galleys and archers.
- **Bombardment,** the galley row's first five entries, stays (§14), but its numbers are the riskiest guess. No land unit strikes back at a galley, and only archers can shoot at one on their own turn, so bombardment could make coastal cities too hard to hold. If it does, those entries come down.

## 8. Fog of war

Fog works as in SPEC §4. For what's new:

- **Units:** a visible enemy unit shows everything, as now, including its cargo and rations. A visible transport shows the units aboard.
- **Cities and sites:** who owns them is public, and so is each site's output and cap. A stockpile or pile shows only while you can see the city or site, as capture progress does (SPEC §3.6).

## 9. Winning

- **Out of cities** still loses (SPEC §3.8). Sites don't count.
- **No faster way to win.** Games are meant to last hundreds or thousands of rounds (§14), so elimination is the way to win.
- **Round limit:** 5,000 rounds, as a backstop for games that stall. The tiebreak counts cities, then sites, then army value.

## 10. Rule constants

New keys, with their values in the strategic preset and in the default game:

| key | strategic | default | meaning |
|---|---|---|---|
| `supply` | 1 | 0 | 1 turns on wheat, bread, and rations (§4, §5), in place of gold |
| `sites` | 32 | 0 | resource sites on generated maps, including the two next to the homes; must be even |
| `water_pct` | 40 | 6 | how much of a generated map is water |
| `start_bread` | 300 | – | bread in each home city at the start |
| `city_bread` | 10 | – | bread a city makes per turn, unless an enemy unit is on or next to it |
| `site_output_min` | 20 | – | the least wheat a site grows per turn |
| `site_output_max` | 100 | – | the most wheat a site grows per turn |
| `site_cap_min` | 400 | – | the smallest cap on a site's pile |
| `site_cap_max` | 3000 | – | the largest cap on a site's pile |
| `cargo_max` | 1000 | – | the most goods a wagon carries, wheat and bread together |
| `passengers_max` | 4 | – | the most units a transport carries |
| `ration_max` | 30 | – | the most rations a unit carries |
| `eat_base` | 1 | – | rations a unit eats each turn |
| `eat_move` | 1 | – | extra rations on a turn it moved |
| `eat_attack` | 1 | – | extra rations on a turn it attacked |
| `ration_cost` | 5 | – | bread per ration refilled |
| `starve_hp` | 10 | – | HP a starving unit loses per turn |

A dash means the key isn't used. With `supply` on, the gold keys (`start_gold`, `city_income`, and `unit_upkeep`) aren't used either. The preset also sets the map to 80 × 60 with 46 neutral cities and the round limit to 5,000, keeps the large preset's clock for now (§14), and adds the site terrain, the naval move cost, and the three unit types.

## 11. Protocol changes

The additions follow the pattern fortification set: new fields go at the end of existing lines, and kits skip kinds of lines they don't know. Kits read a missing field as 0. A bot still needs new code to play this ruleset well.

**Init:**

```
> TERRAIN ~ water 0 0 0 0 1            ... conceal naval_move_cost (0 = ships can't enter)
> TERRAIN R site 1 0 0 0 0
> UNITTYPE W wagon 100 100 4 2 1 1 carry
> SITE 0 15 7 0 30 2400                id q r initial_owner output cap, one per site
> RULE supply 1
```

**Turn state:**

```
> CITY 4 1 0 0 420                     id owner capturer progress bread
> SITE 0 1 0 0 650                     id owner capturer progress wheat
> UNIT 41 1 W 12 9 100 0 0 0 0 30      id owner type q r hp fort carrier wheat bread rations
> LOG SITE 5 2 1                       a site changed hands: id, old owner, new owner
```

- There's no `GOLD` line.
- `carrier` is the transport a unit is aboard, or 0. It's the field SPEC §12 reserved for transport. A unit aboard reports its transport's hex.
- `wheat` and `bread` are a wagon's cargo, and 0 for other units.
- `SEE` lines get the same new fields as `UNIT` lines.

**Orders:**

```
< L <unit> [amount]         load wheat from the site, or bread from the city, that the wagon stands on
< U <unit> [amount]         unload into the city the wagon stands on, wheat first
> OK <wheat> <bread> <left> the wagon's wheat and bread afterward, and what's left at the site or city
```

- `M` also boards and lands (§6), and `C` also captures sites.
- `B` pays in bread from the city's stockpile, and puts ships next to the port.
- New error codes: `no_bread`, for a build the city's stockpile can't pay for; `no_damage`, for an attack whose base damage is 0; and `cannot_carry`, `cannot_load`, and `cannot_unload`, for `L` and `U` orders that can't be carried out. With supply off, `L` and `U` are unknown orders, as before.
- The golden test vectors in [tests/vectors/supply.json](tests/vectors/supply.json) pin the cases down, as orders.json does for the default rules.

**Example.** A wagon at (12,9) drives onto site 0 at (15,7) and loads the 650 wheat piled there:

```
< M 41 0012
> OK 15 7 0
< L 41
> OK 650 0 0
```

A few turns later it reaches city 4, which holds 420 bread. In the same turn, it unloads, so its wheat becomes bread, and takes on 300 bread for an army:

```
< U 41
> OK 0 0 1070
< L 41 300
> OK 0 300 770
```

Elsewhere, a pikeman at (10,8) boards transport 60, which waits offshore at (9,8):

```
< M 52 3
> OK 9 8 0
```

## 12. Building it

### Phases

Each phase is playable on its own, and gets tournaments before the next one starts.

1. **Goods and sieges, on land.** Sites, wheat and bread, wagons, the `L` and `U` orders, rations, and site placement, on the large map with today's water. The tournaments should answer:
   - Does a bot that hauls wheat beat one that ignores it?
   - Do cities with sites next to them hold, and do cities without them fall to sieges? How long do sieges take?
   - How many orders a turn does supply take?
   - How long do games last, in rounds and in real time?
   - What should the site ranges, `city_bread`, `cargo_max`, `ration_max`, and the eating costs be?
2. **Ships.** The naval move cost, ports, transports, galleys, boarding and landing, and maps with more water. The tournaments should answer:
   - Can a player invade an island?
   - Do blockades work?
   - Is bombardment too strong?
3. **The rest.** The viewer and play page, a clock set from the game times phase 1 measures (§14), and a small strategic preset for play by hand (SPEC §5).

### Work

- **Rules:** a config can change the built-in unit types and terrains but can't add new ones. It needs to, so that `configs/strategic.toml` can add the site terrain and the three unit types.
- **Switch:** `supply` turns on wheat, bread, and rations. Ships need no switch: they're unit types with the `naval` flag, and the default game has none.
- **State:** bread per city; output, cap, and wheat per site; each wagon's wheat and bread; each unit's rations and carrier; and whether each unit moved or attacked on its last turn, for eating.
- **Maps:** `SITE` lines in map files, with each site's output and cap, and the generator's water, site placement and richness, and land-and-sea check.
- **Determinism:** deliveries and refills go in id order, so every engine and kit agrees on them.
- **Kits:** the new lines and fields, pathfinding for ships, and helpers for loading and feeding.
- **Golden test vectors** for every new order, upkeep step, and edge case.
- **Viewer and play page:** sites, piles, stockpiles, cargo, passengers, and ships, plus boarding and landing by clicking. The viewer also has to stay quick on replays thousands of rounds long, since it re-simulates every turn (SPEC §8).
- **A reference bot.** None of today's bots know about goods.
- **The benchmark:** a separate leaderboard. The ruleset also suits mode 3 (SPEC §5). Its big decisions, like which sites to take or where to land, play out over many turns, which is where a supervising LLM could help.

## 13. Open questions

None right now.

### Later

Set aside for now:

- **More warship types.** One type may make naval play flat. A fast raider and a heavy ship are the obvious pair.
- **Roads:** terrain that makes wagons faster, which players build, so supply routes become something they make. SPEC §12 already has the hook for changing terrain (`TILE` lines).

## 14. Agreed so far

Settled in discussion on 2026-11-21:

- **A separate ruleset.** The default game stays as it is.
- **Supply is carried.** Goods travel in wagons, which ride transports at sea.
  - *Rejected:* "connected" supply, where a site's goods would reach a city by themselves whenever a path between them avoided enemy units. Nobody would see the route, players would have to learn what blocks it, and the blocking rule would be easy for kits to get subtly wrong.
- **Few carriers that hold a lot,** and sites that store goods for many turns, up to a high cap.
- **Each city keeps its own stockpile.** With one shared pool, a delivery to any city would feed every city, and a siege would only cost some income.
- **Loading and unloading are orders,** like capturing, so a wagon can pass through a city without unloading.
- **Losing a carrier loses its cargo,** which makes escorts worth paying for.
- **Sites are captured like cities,** and the pile goes with the site.
- **Wagons ride on transports.** They're separate units, and neither is amphibious.
  - *Rejected:* an amphibious wagon. It would be odd next to infantry that can't swim, and it would let supply cross water without ships.
  - *Dropped:* transports holding goods themselves. Where there's no port, goods would need an extra order to pass from a wagon to a ship.

Settled on 2026-11-22:

- **Sites differ** in how much they grow and how much they hold.
- **Goods are processed in cities.** Sites grow wheat, which becomes bread only in a city, and units eat only bread. With one kind of goods, an army could live off a site and never need a city, and supply would have no shape.
- **Wheat is the only raw good,** for now and probably for good.
  - *Dropped:* timber, iron, and other goods that only some units would need. There would be more to haul and track.
- **Wagons carry both** wheat and bread.
- **Bread pays for everything.** Units cost bread to build, and there's no gold.
- **Every unit eats, by what it does.** Holding still costs the least, and moving and attacking cost more. A unit carries enough to last a long time.
- **Cities make a little bread,** so a player can never be left unable to build.
- **Long games are intended:** hundreds or thousands of rounds. Elimination stays the way to win, and the round limit is only a backstop.
  - *Dropped:* a faster way to win, such as losing with your home city, or victory points at the round limit.
- **Passengers are shown.** A visible transport shows the units aboard.
  - *Rejected:* hiding them, which would have allowed decoys.
- **Bombardment stays.** Galleys can hit land units on the coast next to them.
- **Wagons can't be seized,** to start. Destroying a wagon destroys its cargo.
- **Later:** more warship types, and roads (§13).
- **Phase 1 keeps the large preset's clock,** 2 seconds a turn, and measures how long games really take. The strategic clock gets set in phase 3 from those numbers.
  - *Why:* long games make the clock matter more than the round count. At 2 seconds a turn, a 2,000-round game can take over 2 hours if both bots use all their time, while bots that answer in a tenth of a second finish one in under 7 minutes. That decides how many tournament games fit in a day.
