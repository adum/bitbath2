# Bitbath: Strategic Ruleset

**Status:** design draft, 2026-11-22. Phases 1 and 2 (§12), supply and ships, are built and playable: `bitbath play --config configs/strategic.toml`, or on the small islands map (§3) with `configs/islands.toml`. Their tournament results are in §12.

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

- **Sites** grow wheat every round and pile it up, to a cap. Each site has its own output and cap. They have no owners: any player's wagon can load the wheat piled on one.
- **Cities** turn wheat into bread the moment it arrives, and make a little bread of their own. Each city keeps its own bread, which pays for the units built there and feeds the units on and around it. There's no gold.
- **Wagons** carry wheat from sites to cities, and bread from cities to armies.
- **Every unit eats** a ration a turn when it holds still, and more when it moves or attacks. It carries 60, and refills them with bread from a city or wagon on or next to its hex. A unit with none left starves.
- **Sieges** follow from these rules. A city under siege makes no bread. If no wagon gets in, it stops building once its stockpile is gone, and its garrison starves once their rations run out. Even a city with a site next to it needs a wagon to bring that site's wheat in.
- **Ships** sail on water, which land units can't enter. Transports carry land units, wagons included. Warships fight at sea, and sinking a transport sinks everything aboard.

## 3. Map

- **Size:** 80 × 60 with 48 cities, the large preset, to start. Larger maps later.
- **Water:** about 45% of the map instead of about 6%, which makes seas, coasts, and islands. Maps stay point-symmetric (SPEC §3.9).
  - Land tends to join up into one mass while the water stays in separate seas, so a few points of water change the maps a lot. Over 100 maps each:
    - at 40%, two maps in three put both homes on one continent with lakes, 27% of the cities need ships, and one map in five has no city that does;
    - at 45%, the homes share a landmass on about half the maps, 41% of the cities need ships, and 7 maps in 100 have none that do. 6 maps in 100 start each home on an island of 4 cities or fewer, its home included, so the war starts at sea;
    - at 50%, 58% of the cities need ships, and 14 maps in 100 start each home on such an island.
  - With more water there's less land for the cities, so the least distance between them shrinks with the square root of the land's share. At 45% water, the 48 cities of the large map sit at least 7 hexes apart instead of 10.
- **Resource sites** are a new terrain, `R`: move cost 1, with no defense, vision bonus, or concealment. The generator places `sites` of them in mirrored pairs:
  - one next to each home city;
  - half of the other pairs, rounded down, next to neutral cities, at most one per city, which makes those cities fortresses worth fighting over;
  - the rest in the open, at least 3 hexes from any city, where only wagons can collect them. Some of these are on islands.
- **Each site has its own output and cap,** which the generator picks from the ranges in §10. The two sites of a mirrored pair match.
  - The sites next to the homes get the middle of each range. Sites get richer the farther they are from the nearer home, so the best ones are in the middle of the map and on islands. A neutral city next to a rich site is the biggest prize on the map, since its wagons have the shortest haul.
  - Output and cap are picked separately, so sites come in kinds. A *fast* site, with high output and a small cap, fills up quickly, so it wants frequent visits. A *deep* site, with low output and a big cap, can be left alone for 50 rounds and emptied in one haul, by whichever player's wagon gets there first.
- **Validation:** instead of every city being reachable by land (SPEC §3.9), every city and site must be reachable from both homes by land and sea, and each home's landmass must have a port (§6).
  - A sea counts as reachable once a player can reach a city next to it, which could build the ships.
  - A home is never a port itself, since the generator clears the impassable hexes around homes, water included. A player's first port has to be taken.
- **Start:** each player gets `I, I, S` and a wagon on their home city, and `start_bread` in it.
- **The default game's maps don't change.** The new generator parameters default to today's behavior.
- **The islands map** is a small map for testing the resources and ships before the large maps, picked with `map_layout = "islands"`. [configs/islands.toml](configs/islands.toml) plays it at 40 × 30 with the strategic preset's rules.
  - Each home island spreads into its corner of the map and along the map's edges, about 130 to 160 hexes at 40 × 30, so it has a long coast to land on. The home stands well inland, toward its corner: 6 or 7 hexes from the sea at 40 × 30, so an army that lands on the island has a march ahead of it. Its site is on the island 4 or 5 hexes from the home and at least 4 from the port, so its wheat takes a wagon to bring in. A neutral port stands on the coast facing the middle of the map, 5 to 8 hexes from the home. Taking the port is the opening.
  - The middle of the map is an island with no city, holding the other sites in one patch. The two at the very middle touch: no hex is exactly at the middle, since maps are mirrored by a 180° turn and have an even height. Only wagons carried by transport can bring their wheat home.
  - The sites by the homes get the middle of each range, and the sites in the middle the top of it: 100 wheat a round each, piling up to 3,000.
  - Everything else is sea. The seed shapes the coasts and terrain, and picks the port and the homes' sites.
  - It needs `neutral_cities = 2`, `sites` of at least 4, and ships. `water_pct` doesn't apply.

## 4. Wheat and bread

There are two kinds of goods. **Wheat** grows at sites. **Bread** is made from it in cities: wheat becomes bread, one for one, the moment it enters a city. Bread builds units and feeds them, and nothing eats wheat, so an army can't live off a site. It needs a city behind it.

### Sites

- **No owners:** a site belongs to no one, and no one can capture it. A `C` order on a site is refused.
- **Production:** at the start of each round, in player 1's upkeep, every site adds its output of wheat to its pile, up to its cap, whoever stands on it or next to it.
- **Loading:** any player's wagon on a site can load what's piled there (Wagons below). A unit standing on a site keeps the other player's wagons off it, since they can't enter its hex, so a raider can cut off a site by standing on it.
- **Vision:** a site gives no one vision.

### Cities

- **Stockpile:** each city stores any amount of bread.
- **Making bread:** at the start of its owner's turn, a city makes `city_bread` bread, unless an enemy unit stands on it or next to it. Neutral cities make none.
  - This keeps a player from ever being stuck. One with no sites and no wagons can still build, slowly: on its own, a city builds a scout in 8 turns, or infantry or a wagon in 10.
  - It doesn't help a city under siege, which has to live on its stockpile and the wagons that get through. Without that exception, 10 bread a turn would keep two units holding still alive in every besieged city forever.
  - A single enemy unit next to a city, even a scout, stops its bread. That's cheap harassment, but it costs the city only `city_bread` a turn, and the unit is easy to kill.
- **Building** pays the unit's cost in bread from that city's stockpile. Otherwise it works as in SPEC §3.5. To build near the front, send bread there first.
- **Capture:** whoever captures a city takes its stockpile.
- **No gold:** there's no shared pool, gold income, or gold upkeep. Neutral cities start empty.

### Wagons

- **Capacity:** a wagon carries up to `cargo_max` goods, wheat and bread together.
- **Loading:** `L <wagon>` takes wheat from the site the wagon stands on, whoever's wagon it is, or bread from the city it stands on, which its player must hold, as much as fits.
- **Unloading:** `U <wagon>` puts the wagon's goods into the city it stands on, which its player must hold. The wheat becomes bread. A wagon can also carry bread from one city to another.
- **Amounts:** both orders take an optional amount, to move at most that much, for instance to leave some bread in a city. Unloading takes wheat first.
- **One stop:** a wagon can load and unload as often as it likes in a turn, but can't move after doing either. So a wagon can drive into a city, unload its wheat, and take bread on, all in one turn.
  - A wagon passing through a city delivers nothing.
  - Even a site next to a city takes two turns per round trip: one to load, and one to step into the city and unload.
- **Losing a wagon loses its cargo.** The goods aren't dropped or captured.
- **Feeding:** wagons also feed the units on and next to them with their bread (§5). Wheat feeds no one.

## 5. Food and sieges

**Rations.** Every unit carries up to `ration_max` rations. New units start full.

**Eating.** Each turn costs a unit `eat_base` rations, plus `eat_move` if it moved and `eat_attack` if it attacked. With the starting values, holding still costs 1, moving or attacking 2, and both 3.

- Moving means changing hexes, as it does for fortification (SPEC §6.6), so boarding and landing count. Riding aboard a moving transport doesn't.
- Only attacks on the unit's own turn count. Striking back costs nothing extra, and neither does capturing.
- A full unit can hold still for 60 turns, march for 30, or march and fight for 20. Thirty turns of marching take infantry farther than the map is wide, on open ground.
- Holding still is cheap, which suits defense: a unit holding a city digs in and eats the least. Attacking costs the most, so an offensive needs supply.

**Upkeep** at the start of each turn runs in this order, in place of income (SPEC §3.4):

1. **Production:** in player 1's upkeep, every site grows wheat, so sites grow once a round. Then the player's cities make bread (§4).
2. **Feeding:** each of the player's units eats what its last turn cost, then refills to `ration_max` from its player's cities and wagons on its hex or next to it, paying `ration_cost` bread per ration.
   - Units are served in id order.
   - Each takes from its sources in turn, cities before wagons and lower ids first, until it's full or they run out. A source pays for whole rations only.
   - A unit that can't cover what it ate, from its rations and a refill together, is left with none and starves: it loses `starve_hp` HP, and can die of it. So a unit that reaches bread in time never starves.
3. **Healing,** as in SPEC §3.4, except that a unit that starved this turn doesn't heal. Ships heal next to their own cities (§6).
4. **Capture reset, refresh, and vision,** as in SPEC §3.4.

**What it costs.** Every ration is paid for in bread when the unit refills, at `ration_cost` each. With the starting values, a unit holding still costs 5 bread a turn, the same as gold upkeep in the default game. One that moves costs 10, and one that moves and attacks costs 15. So bread limits the army a player can keep, and an offensive costs more than holding a line. If cavalry raids turn out too cheap, a per-type appetite, such as cavalry eating double, is the obvious next knob.

**Sieges** come out of these rules. With the starting values:

- Say a city holds 300 bread and 4 units, and the besiegers stand next to it, so it makes no bread. Holding still, the garrison eats 4 rations a turn, which costs 20 bread. If no wagon gets in, the stockpile lasts 15 turns.
- Then the garrison lives on its rations for 60 turns, and then starves, at 10 HP a turn. Starving units also hit weaker, since damage scales with HP.
- A city with a site next to it can live on that site's wheat, if a wagon keeps bringing it in. A site that grows 50 a round feeds 10 units holding still. To starve such a city, the besieger has to stand on the site or stop the wagon.
- Besiegers eat too. Holding still, they last 60 turns on their rations, and starving a city out takes 75 turns or more, so a siege that waits needs wagons behind the lines, which a relief force can raid.
- **Starving a city out is slow,** which suits games meant to last hundreds or thousands of rounds. If sieges drag even so, `ration_max` is the knob.

**In the field,** an army needs wagons of bread behind it or stocked cities along the way, so how far it can go depends on its supply. A captured city comes with its stockpile and starts making bread, so taking cities along the way helps feed an advance.

## 6. Ships

### Water and ports

- **Naval move cost:** terrain gets a second move cost, for ships. Water's is 1, and every other terrain's is 0, which means ships can't enter it. Land units still can't enter water.
- **Naval units** have the `naval` flag and move by the naval move cost. Otherwise ships move as land units do: paths, stacking, zones of control, and ambushes all work the same (SPEC §3.5).
- **Ports:** a city next to water is a port. Ships don't enter cities.
  - A port builds a ship onto the first water hex next to it, in direction order (SPEC §3.1), that has room and no enemy units. A hidden enemy unit counts too: the ship goes to the next such hex.
  - A ship next to one of its player's cities is in port. It heals there by `city_heal` and refills its rations from that city (§5).

### Transports

- **Capacity:** a transport carries up to `passengers_max` land units, wagons included.
- **Boarding:** a land unit boards by moving onto the transport. As its last step, a move may enter a water hex that holds one of its player's transports with room. The boarding step costs 1 MP, and boarding ends the unit's move. If several of the player's transports there have room, the unit boards the lowest-numbered.
- **Aboard,** a unit moves with the transport.
  - It can't attack, capture, load, or unload, and it can't be attacked. Only the transport can.
  - It doesn't count toward the hex's stack limit, exerts no zone of control, and doesn't dig in.
  - It still sees from the transport's hex, and still eats. A wagon aboard feeds the units around it, like any wagon.
- **Landing:** a unit aboard gets off with a one-step move onto land next to the transport, which can be a city. That's the only move it can make: it can't step across to another transport.
  - Landing ends the unit's move but leaves its action, so it can attack or capture where it lands, as after a move that ends in a zone of control.
  - Passengers can land after their transport moves in the same turn, so a transport can sail and unload in one turn.
  - A unit can't board and land in the same turn.
- **Sinking:** when a transport is destroyed, everything aboard is destroyed with it, cargo included.
- **Goods at sea** always ride in wagons. Transports don't hold goods themselves (§14).

### Warships

- **The galley** (`G`, §7) is the one warship type to start with. It fights other ships, and can bombard land units on the coast next to it.
- **Zones of control** reach across the shoreline both ways. A line of galleys can close a strait, and pikemen on a beach slow a landing.
- **Blockades** need no rule of their own. Galleys off a port sink the transports that come out, and their zones of control stop transports from slipping past.
- **Fleets eat too.** A ship that sails every turn runs out after 30 turns away from port, so a longer voyage needs a wagon of bread aboard a transport nearby.
- **From land,** only archers can hurt ships (§7).
- **Ships need supply.** Naval unit types and naval move costs are allowed only with `supply` on, so there's one switch for the strategic ruleset's protocol. Units start on the home city, so start units must be land units.

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
- **Bombardment,** the galley row's first five entries, stays (§14), but its numbers are the riskiest guess. No land unit strikes back at a galley, and only archers can shoot at one on their own turn, so bombardment could make coastal cities too hard to hold. If it does, those entries come down. The first naval tournament found it weak instead: the bot that bombarded did only about 3% of its damage to land units that way (§12).

## 8. Fog of war

Fog works as in SPEC §4. For what's new:

- **Units:** a visible enemy unit shows everything, as now, including its cargo and rations. A visible transport shows the units aboard.
- **Cities and sites:** who owns each city is public, and so is each site's output and cap. A stockpile or pile shows only while you can see the city or site, as capture progress does (SPEC §3.6).

## 9. Winning

- **Out of cities** still loses (SPEC §3.8). Sites don't count.
- **No faster way to win.** Games are meant to last hundreds or thousands of rounds (§14), so elimination is the way to win.
- **Round limit:** 5,000 rounds, as a backstop for games that stall. The player with more cities wins. With equal cities, the bigger army wins: each unit counts its cost times the share of its HP it has left, and all the bread a player holds, in its cities and its wagons, counts too. Equal on both is a draw.

## 10. Rule constants

New keys, with their values in the strategic preset and in the default game:

| key | strategic | default | meaning |
|---|---|---|---|
| `supply` | 1 | 0 | 1 turns on wheat, bread, and rations (§4, §5), in place of gold, and allows ships (§6) |
| `sites` | 32 | 0 | resource sites on generated maps, including one near each home; must be even |
| `water_pct` | 45 | 6 | how much of a generated map is water |
| `map_layout` | 0 | 0 | the map generator: 0, `noise`, for the maps of SPEC §3.9 and above; 1, `islands`, for the islands map (§3). A config can give the name |
| `start_bread` | 300 | – | bread in each home city at the start |
| `city_bread` | 10 | – | bread a city makes per turn, unless an enemy unit is on or next to it |
| `site_output_min` | 20 | – | the least wheat a site grows per round |
| `site_output_max` | 100 | – | the most wheat a site grows per round |
| `site_cap_min` | 400 | – | the smallest cap on a site's pile |
| `site_cap_max` | 3000 | – | the largest cap on a site's pile |
| `cargo_max` | 1000 | – | the most goods a wagon carries, wheat and bread together |
| `passengers_max` | 4 | – | the most units a transport carries |
| `ration_max` | 60 | – | the most rations a unit carries |
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
> TERRAIN ~ water 0 0 0 0 1            ... conceal naval_move_cost (0 = ships can't enter); only with supply on
> TERRAIN R site 1 0 0 0 0
> UNITTYPE W wagon 100 100 4 2 1 1 carry
> SITE 0 15 7 0 30 2400                id q r owner output cap, one per site, where owner is always 0
> RULE supply 1
```

**Turn state:**

```
> CITY 4 1 0 0 420                     id owner capturer progress bread
> SITE 0 0 0 0 650                     id owner capturer progress wheat, where the middle three are always 0
> UNIT 41 1 W 12 9 100 0 0 0 0 60      id owner type q r hp fort carrier wheat bread rations
```

- There's no `GOLD` line.
- Sites have no owners, but `SITE` lines keep their owner fields, always 0, so kits written when sites had owners still read them.
- `carrier` is the transport a unit is aboard, or 0. It's the field SPEC §12 reserved for transport. A unit aboard reports its transport's hex.
- `wheat` and `bread` are a wagon's cargo, and 0 for other units.
- `SEE` lines get the same new fields as `UNIT` lines.

**Orders:**

```
< L <unit> [amount]         load wheat from the site, or bread from the city, that the wagon stands on
< U <unit> [amount]         unload into the city the wagon stands on, wheat first
> OK <wheat> <bread> <left> the wagon's wheat and bread afterward, and what's left at the site or city
```

- `M` also boards and lands (§6). A boarding move's `OK` gives the transport's hex and 0 MP left, and a landing's the hex landed on and 0 MP. A landing cut short by a hidden enemy is `PART` at the transport's hex.
- `C` on a site is refused with `cannot_capture`, since sites have no owners.
- `B` pays in bread from the city's stockpile, and puts ships next to the port. Its reply is `OK <id>`, as for any build, without the hex the ship went to.
- New error codes:
  - `no_bread`, for a build the city's stockpile can't pay for;
  - `no_damage`, for an attack whose base damage is 0;
  - `cannot_carry`, `cannot_load`, and `cannot_unload`, for `L` and `U` orders that can't be carried out;
  - `aboard`, for an attack, capture, load, or unload by a unit aboard a transport, or an attack on one;
  - `no_port`, for a ship built by a city that isn't next to water.
- With supply off, `L` and `U` are unknown orders, as before.
- The golden test vectors in [tests/vectors/supply.json](tests/vectors/supply.json) and [tests/vectors/ships.json](tests/vectors/ships.json) pin the cases down, as orders.json does for the default rules.

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

### Phase 1 results so far

The first tournament, on 2026-11-22: the reference bot against itself with hauling turned off (`--no-haul`), on 5 maps, each map twice with the sides swapped, at most 300 rounds a game, with 30 rations, the preset's value then, and with 60.

| `ration_max` | hauler's record | ended by elimination | game length, rounds | hauler's army | walking to bread | starving |
|---|---|---|---|---|---|---|
| 30 | 9–1 | 3 of 10 | 160 to 300 | 152 units | 50% | 20% |
| 60 | 10–0 | 10 of 10 | 87 to 197 | 203 units | 22% | 7% |

The army and the shares walking to bread and starving are the hauler's averages over the second half of each game.

- **Hauling wins.** The hauler won 19 of 20 games. Its bread income was about 2.6 times the other bot's with 30 rations, and 5 times with 60.
- **With 30 rations, the bot can't keep an offensive fed.** Half its army is walking back to bread at any time, while its cities hold over 14,000 bread it can't get to the front. Most games reach the round cap. With 60 rations, offensives work and every game ends by elimination. This may be the bot as much as the rules: better feeding wagons would help at 30. The preset now uses 60 (§14).
- **Supply takes few orders.** The hauler sends about 2.5 `L` and `U` orders a turn, among about 140 moves. It thinks for 0.4 to 0.7 seconds a turn, and a 300-round game takes about 4 minutes.
The second tournament played the reference bot against itself with 60 rations, on 4 maps, at most 1,000 rounds a game. The bot plays the same way every time, so each map's two games were mirror images: 4 different games.

- **Even games ended by elimination in 129 to 283 rounds.** One side gets ahead and snowballs.
- **They took 1 to 3 minutes of thinking in all,** about 0.15 to 0.45 seconds a turn per bot, with no turn over 0.8 seconds.
- **About 64 units starved to death per game,** and about 4% of the army was starving at any time.
- **Cities fall by assault, not hunger.** A replay analysis counted every stretch of turns a city spent with enemy units on it or next to it, across both tournaments. Such a siege ended, with the city held or taken, after a median of 1 to 2 rounds. Of 742 sieges in the even games, 4 lasted 10 rounds or more. Starving out a garrison of 4 in a city with 300 bread would take over 75 turns, so it never decided anything.
- **A site next to a city helps it hold.** In the even games, 41% of sieges of a city with its owner's site next to it ended with the city taken, against 56% for other cities. Part of that may be where those cities are, as each home city has one.

So with today's numbers, supply decides how far armies can reach, but not sieges. Making sieges matter would take garrisons that run out of food far sooner than they do now.

The third tournament added Forager, a simpler bot in Rust with a different strategy (§12 Work), on 2 maps, both sides, at most 400 rounds a game:

- **Hauling wins with a second strategy too.** The reference bot won all 8 of its games, and Forager won all 4 of its games against the bot that doesn't haul.
- **11 of 12 games ended by elimination,** in 151 to 276 rounds.
- **Forager thinks for about 16 milliseconds a turn,** against about 400 for the reference bot.
- **Big armies still starve.** The reference bot lost about 141 units a game to hunger and Forager 91, while the bot that doesn't haul, with a smaller army kept near its cities, lost 9.

### Phase 2 results so far

The first naval tournament, on 2026-11-22, played three bots on 4 maps with islands, at 40% water, before the preset moved to 45% (§14), each map twice with the sides swapped, at most 400 rounds a game:

- **naval,** the naval reference bot (see Work below);
- **nobombard,** the same bot with `--no-bombard`, so its galleys never attack land units;
- **supply,** the phase 1 reference bot, which builds no ships.

On two of the maps (seeds 3 and 6) the homes are on separate landmasses, and on the other two (seeds 5 and 11) they share one, with islands offshore. All 24 games reached the round limit. Cities decided 22 of them, and army value the 2 that tied on cities.

| | homes apart | homes together |
|---|---|---|
| a bot with ships against supply | 7–1 | 2–6 |
| naval against nobombard | 2–2 | 2–2 |

- **Islands fall early.** The bots with ships took their first city across the water between rounds 19 and 52, and about 11 such cities a game in all.
- **Invading the enemy's own landmass works, slowly.** Against supply, with the homes apart, the bots with ships landed about 125 units a game away from home and took 7 or 8 cities on the enemy's home landmass. None came close to eliminating the enemy in 400 rounds.
- **A navy pays across water and costs on land.** On seed 6, with the homes apart, the bots with ships beat supply in all 4 games, from either side. On seed 5, with the homes together, supply beat them in all 4, by as much as 34 cities to 9. They spent bread, units, and wagons on islands and ships, while supply put everything into the war on land. On the other two maps the side mostly decided. A better bot would build a navy only where its war needs one.
- **Archers rule the shore.** A transport has to come next to a coast to land its units, and archers there can shoot it from 2 hexes away, where nothing on it can strike back. On the maps with the homes apart, supply's archers sank about 18 transports a game, with 37 units aboard, and 13 to 22 galleys. Between the two naval bots, galleys sank only 1 to 3 transports a game.
- **Blockades barely bite.** A port had an enemy ship next to it 3% to 7% of the time. That cost its owner 800 to 2,500 bread a game, well under 1% of the 350,000 to 580,000 bread a player made.
- **Bombardment is weak, not too strong.** Naval's galleys did about 3% of the damage it dealt to land units, and killed fewer than 3 a game. Naval and nobombard split their 8 games 4–4, and on each map the same side won both. Bombarding cost naval more galleys: it lost 5.0 a game to nobombard's 1.4.
- **Ships starve without a plan for food.** Early versions of the naval bot lost a dozen ships a game to hunger, many with their crews aboard: loaded convoys waiting by a port ate its bread faster than it was made. With wagons stocking the ports, and transports that put their units back ashore when food runs short, about 1 ship a game starved, with almost no one aboard.
- **Player 1 won 17 of 24 games.** Swapping the sides cancels that in the totals, but on seeds 3 and 11 the side decided nearly every game.
- **Time:** the bots thought for about 0.28 seconds a turn on average and at most 1.3 seconds, so a 400-round game took about 4 minutes of thinking.

So with these bots, ships decide a war across water, and islands are easy prizes. Coastal archers, not galleys, are what stops an invasion. Bombardment doesn't need toning down.

**Small islands, at 45% water.** After the preset moved to 45% (§14), a check on seed 90, which starts each home on an island with one other city, found the naval bot stuck there.

- **Why:** with its island taken, its army filled what its bread income could feed, so it built no ships. That army also stood on its home city, the only one with bread to spare, so its wagon could never get in to load bread for the port. Both games against supply were draws at round 400, with 2 cities each and over 9,000 bread unspent.
- **The fix:**
  - Ships can now live on the bread stocked in the ports too, one more ship for every 600.
  - A wagon that comes to load or unload at a city full of its own units first moves one of them aside.
  - A wagon stocks a port only from a city that can spare 800 bread beyond what it keeps. Ports keep extra bread for their ships, so before, a wagon could stand at such a port for good, sending invalid orders.
- **On small islands:** 6 of the first 100 maps start each home on an island of 4 cities or fewer. On those, the fixed bot beat supply in all 12 games: 8 by elimination, between rounds 149 and 363, and the other 4 by 44 or 45 cities to 3 or 4. It took its first city across the water between rounds 24 and 126.
- **On ordinary maps** the fix changes little. On 6 of them, the fixed bot beat the bot from before it 7–5, and sent no invalid orders, where the old one sent 48.
- **Still to fix:** on seed 5, the second player's ships starved by the dozen with either version, up to 24 in a game. And in 24 games between the naval bots on those maps, player 2 won 21, so the side still decides many of them.

**The islands map** (§3), on 2026-11-23. These games were on the map's first version, whose home islands were round, about 40 to 60 hexes, before they spread into their corners. The naval bot as it was never went near the middle island, since its expeditions sailed only for cities. In a game between two of them, each took its port, then player 2 landed on player 1's island and won by elimination in round 38. So the bot learned three things:

- **Sites overseas.** Sites it doesn't hold, on landmasses where no one holds a city, are destinations for its expeditions, so that a ferry could haul from them. A site is worth as much as a neutral city for every 100 wheat a turn it grows. Its own sites there count too, once enemy units land.
- **Ferries.** On such a landmass, the units that took the sites stay by them as guards, and a transport ferries 1 to 4 wagons between the sites and a port. The first wagon of each trip takes bread for the guards, and all of them load wheat at the sites and unload it in the port.
- **Garrisons.** If its home is on an island of 4 cities or fewer, expeditions leave 2 land units there for each city. Without them, the old bot beat the new one 18–2. While the new bot's army took the middle, the old one landed on its empty home island and won by elimination in about 30 rounds. A larger home landmass has the land army to defend it.

Then the new bot played on 10 islands maps, each twice with the sides swapped, at most 500 rounds a game:

- **Against the old bot it won 18–2.** 17 of its wins came by elimination, in rounds 39 to 391, and 1 at the round limit with 2 cities each, by the sites tiebreak, with the middle in its hands. The old bot's 2 wins were invasions, in rounds 54 and 65.
- **Between two new bots, the middle decides most games.** On 8 maps, 7 games ended by elimination, in rounds 76 to 186. Each winner had ferried home 7,400 to 18,600 wheat, and in 6 of the 7 the loser had ferried none. The eighth reached round 500 with 2 cities each, and the side holding more sites won, though the other had ferried 8,300 wheat.
- **The side decides who gets the middle.** Player 2 won 6 of the 8. Both convoys land there in the same round, player 1's first. Then, in 7 of the 8 games, player 2 struck first, 2 to 6 rounds before player 1 attacked at all. It's the same side bias as on the large maps, plainer.
- **Time:** about 27 milliseconds a turn on average, and at most 119.
- **On the large map, little changes.** On 6 maps, each twice with the sides swapped, at most 400 rounds a game, the new bot and the old one split 6–6, with 286 cities to 290. Earlier versions went 3–5 and 5–7 there: both kept a garrison on every landmass, and one also sent expeditions for sites on the enemy's own landmass.

**Forager at sea**, on 2026-11-23, after the home islands spread into their corners. Until then only the naval bot could leave its island on the islands map, so Forager learned ships in its own way (Work below).

- **Against the naval bot on the islands map, it's even: 8–8,** on 8 maps, each twice with the sides swapped, at most 500 rounds a game. The map mattered more than the side. Each bot won both games on 3 maps, and the other 2 maps split by side. Forager won 4 games by elimination, the earliest in round 43, and 4 at round 500.
- **On the large map, it beat the old Forager 6–2,** on 4 maps, both sides, at most 400 rounds a game. That includes 40 cities to 8 and 36 to 12 on two of the maps. The old bot won both games on the fourth map, by 2 cities and on army value.
- **Without ships, it plays as before.** It sent the same orders, move for move, in 3 default games and 3 supply games without ships.
- **Time:** about 1 millisecond a turn on the islands map and 7 on the large map, and at most 25.

**Neutral sites**, on 2026-11-24 (§14). Sites lost their owners, a site next to a city stopped delivering by itself, and the tiebreak became cities, then armies. The bots changed with the rules:

- **The supply and naval bots** haul from every site on their side of the map, no farther from their cities than from the enemy's, and no longer send capturers for sites. The naval bot's expeditions sail for sites overseas until its units stand on their landmass, and its ferries serve any landmass with sites where its units stand and no one holds a city.
- **Forager** sends its capturers only for cities, raids the sites on the enemy's side of the map, and hauls and ferries as the others do.
- **Two old bugs showed up.** A wagon with wheat to unload looked only for a city with bread to spare, so once its home city ran low it never came home. Before, the home site kept that city stocked. And since a unit boards the lowest-numbered transport on a hex, Forager's wagons could board a troop transport docked by their ferry and sail off with it. Wagons now unload in any city, and Forager's transports dock apart.
- **On 6 islands maps, both sides, at most 500 rounds a game, Forager beat the naval bot 10–2,** where they were even, 8–8, before. 11 games ended by elimination, in rounds 49 to 180, and 1 at round 500, on army value with 2 cities each. Neither bot sent an invalid order.
- **Why:** the middle island's sites now grow from round 1, so whoever lands there first finds thousands of wheat piled up. In the shortest game, Forager's ferry brought home 1,000 wheat a trip from round 20, while the naval bot kept its army to what its home site and cities fed, 9 units, and waited for a convoy.
- **With the homes moved inland and their sites 4 or 5 hexes out** (§3), on the same 6 seeds, Forager won all 12 games, by elimination in rounds 56 to 143, and neither bot sent an invalid order. In the first 40 rounds the naval bot's wagons brought in about all its home site grew, 2,200 to 2,400 wheat, while Forager loaded over 5,000, most of it in the middle.

### Work

Done in phase 1:

- **Rules:** a config can add unit types and terrains (`unit.<code>` and `terrain.<char>`, as configs/default.toml describes), and [configs/strategic.toml](configs/strategic.toml) adds the site terrain and wagons.
- **Switch:** `supply` turns on wheat, bread, and rations. With it off, the default game's messages don't change at all.
- **State:** bread per city; output, cap, and wheat per site; each unit's wheat, bread, and rations; and whether it moved or attacked on its last turn, for eating.
- **Maps:** `SITE` lines in map files, and the generator's site placement and richness (§3).
- **Determinism:** deliveries and refills go in id order.
- **The Python kit:** the new lines and fields, `load()` and `unload()`, and helpers for feeding.
- **Golden test vectors** in tests/vectors/supply.json, and random self-play with supply on.
- **The viewer** draws sites and shows bread, wheat, and rations, and the runner's summaries show bread instead of gold.
- **A reference bot,** [bots/supply_bot.py](bots/supply_bot.py): vanguard's fighting, plus building with each city's bread, hauling wheat from the sites on its side of the map, and feeding the army. `--no-haul` turns hauling off.
- **A second, simpler bot,** [bots/forager_bot.rs](bots/forager_bot.rs), in Rust: it spreads out to take cities, raids enemy wagons and the sites on the enemy's side with cavalry and scouts, holds its frontier cities with pikemen, and thinks for a few milliseconds a turn.

Done in phase 2:

- **Rules:** terrain gets a naval move cost, and unit types the `naval` and `transport` flags, with the constants `passengers_max` and `water_pct`. The preset gives water a naval move cost of 1 and adds transports and galleys (§7).
- **Engine:** ports, boarding, riding, landing, and sinking. Units aboard stay out of stacks, zones of control, and combat. Ships heal and refill in port, and `UNIT` lines give the carrier. The new error codes are `aboard` and `no_port`.
- **Maps:** `water_pct`, the land-and-sea check, and a gap between cities that shrinks with the land's share (§3). Maps without transports don't change.
- **The Python kit:** pathfinding for ships, boarding and landing in `reachable()`, passengers that ride and sink with their transport, and helpers for ports.
- **Golden test vectors** in tests/vectors/ships.json, and random self-play that boards, lands, and sinks.
- **The viewer** draws ships, shows how many units a transport carries, and describes ships in its rules panel.
- **A naval reference bot,** [bots/naval_bot.py](bots/naval_bot.py): the supply bot, plus convoys of transports that carry the idle part of its army overseas, galleys that sink transports, bombard the coast, escort, and blockade, and wagons that stock its ports with bread for the ships. `--no-bombard` keeps its galleys off land units. Vanguard and the supply bot now cope with enemy ships.
- **The islands map** (§3), a small map for testing the resources and ships, picked with `map_layout`. For it, the naval bot learned to land by sites overseas, to ferry their wheat home, and to leave a garrison behind (results above).
- **Forager at sea.** Each of its transports fills up with land units that have nothing left to do at home and sails alone for the best city or site overseas, preferring ones no other transport is after. It ferries wheat from sites overseas as the naval bot does. Its galleys raid enemy transports and otherwise lie off enemy ports. Its wagons carry bread to a port that's saving for a ship, and it keeps a garrison on a small home island.

Still to do:

- **The JavaScript kit and the play page:** the kit skips the new lines and fields safely, but can't play supply or ship games yet.
- **Viewer:** staying quick on replays thousands of rounds long, since it re-simulates every turn (SPEC §8).
- **The benchmark:** a separate leaderboard. The ruleset also suits mode 3 (SPEC §5). Its big decisions, like which sites to hold or where to land, play out over many turns, which is where a supervising LLM could help.

## 13. Open questions

- **Should archers hurt transports less?** In the first naval tournament, on the maps with the homes apart, archers on the shore sank about 18 transports a game, with 37 units aboard (§12). That makes a defended coast very costly to land on. A more careful bot might clear the archers with galleys first, or land out of their reach. If not, archers' damage to transports, now 40, is the knob.

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
- **Sites are captured like cities,** and the pile goes with the site. *Replaced* on 2026-11-24: sites have no owners (below).
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
- **Units carry 60 rations,** after the first tournament (§12). With 30, half the army spent its time walking back to bread.
- **Long games are intended:** hundreds or thousands of rounds. Elimination stays the way to win, and the round limit is only a backstop.
  - *Dropped:* a faster way to win, such as losing with your home city, or victory points at the round limit.
- **Passengers are shown.** A visible transport shows the units aboard.
  - *Rejected:* hiding them, which would have allowed decoys.
- **Bombardment stays.** Galleys can hit land units on the coast next to them.
- **45% water** (§3), after phase 2. At 40%, two maps in three put both homes on one continent with lakes, and one in five needed no ships at all.
- **Wagons can't be seized,** to start. Destroying a wagon destroys its cargo.
- **Later:** more warship types, and roads (§13).
- **Phase 1 keeps the large preset's clock,** 2 seconds a turn, and measures how long games really take. The strategic clock gets set in phase 3 from those numbers.
  - *Why:* long games make the clock matter more than the round count. At 2 seconds a turn, a 2,000-round game can take over 2 hours if both bots use all their time, while bots that answer in a tenth of a second finish one in under 7 minutes. That decides how many tournament games fit in a day.

Settled on 2026-11-23:

- **A small islands map first** (§3), to test the resources and ships before building out much larger maps: two homes on islands in opposite corners, each with a port and a site, and one rich patch of sites on an island in the middle with no city.

Settled on 2026-11-24:

- **Sites have no owners.** No one captures a site. Each grows wheat every round, whoever is near it, and any player's wagon on it can load what's piled there.
  - *Dropped:* capturing sites, and a site next to its owner's city delivering its wheat by itself. Every load of wheat now comes in a wagon, as agreed on 2026-11-21 for supply in general.
- **The tiebreak at the round limit is cities, then armies.** An army counts each unit at its cost times the share of its HP it has left, plus all the bread in its player's cities and wagons.
  - *Replaced:* cities, then sites, then army value without the bread.
- **On the islands map, homes stand further inland and their sites farther out** (§3). A home is 6 or 7 hexes from the sea at 40 × 30, where it was 4 or 5, and its site 4 or 5 hexes from it and at least 4 from its port, where it was next to the home.
