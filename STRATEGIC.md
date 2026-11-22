# Bitbath: Strategic Ruleset

**Status:** design draft, 2026-11-22. None of it is built yet.

- The choices agreed so far are listed in [§14](#14-agreed-so-far). Everything else is a proposal.
- All numbers are starting values, to be tuned by tournaments as the default rules were.
- The questions that still need a decision are in [§13](#13-open-questions).
- "SPEC §n" means a section of [SPEC.md](SPEC.md).

## 1. Goals

The default game is tactical: a 40 × 30 map, short distances, and one shared pool of gold. The strategic ruleset is a second game on the same engine, about territory and supply:

- **A big, open map** with long distances, where holding ground and keeping it supplied matter more than any single fight.
- **Resource gathering:** goods come from a few sites and have to be carried to cities.
- **Sieges:** a city that can't be supplied weakens and falls. A city with its own supply is a fortress.
- **Ships:** transports, warships, and islands worth invading.

The default game stays as it is.

**A ruleset, not a mode.** SPEC §5 uses *mode* for who is playing. The strategic game is a *ruleset*: a config file that turns on the rules below. It's played with the same engine, protocol, runner, viewer, and modes as the default game.

**Principles:**

- **Supply is visible.** Goods travel in units on the map, which can be seen, chased, and escorted. Nothing arrives by an invisible route (§14).
- **Few, big carriers.** Sites store many turns of output, and one wagon holds a full site's worth. Keeping an empire supplied should take a handful of orders a turn.
- **Same machinery.** Combat, zones of control, capture, fog of war, fortification, and the determinism rules (SPEC §12) carry over. New mechanics are data where possible: terrain columns, unit flags, and rule constants.
- **Readable from the rules message.** Everything a bot needs still comes in the init message (SPEC §6.2), so rule variants keep working.

## 2. Overview

- **Sites** produce goods every turn and pile them up, to a cap. They're captured like cities.
- **Wagons** load goods at sites and unload them in cities.
- **Each city has its own stockpile.** It pays for the units built there and feeds the units on and around it. There's no gold.
- **Every unit eats** one ration a turn. It carries 10, and refills them from a city or wagon on or next to its hex. A unit with none left starves.
- **Sieges** follow from these rules. A city no wagon can reach stops building once its stockpile is gone, and its garrison starves once their rations run out. A city with a site next to it is supplied every turn, with no wagon.
- **Ships** sail on water, which land units can't enter. Transports carry land units, wagons included. Warships fight at sea, and sinking a transport sinks everything aboard.

## 3. Map

- **Size:** 80 × 60 with 48 cities, the large preset, to start. Larger maps later.
- **Water:** about 40% of the map instead of about 6%, which makes seas, coasts, and islands. Maps stay point-symmetric (SPEC §3.9).
- **Resource sites** are a new terrain, `R`: move cost 1, with no defense, vision bonus, or concealment. The generator places `sites` of them in mirrored pairs:
  - one next to each home city, owned by that player from the start;
  - some next to neutral cities, which makes those cities fortresses worth fighting over;
  - the rest in the open, away from cities, where only wagons can collect them. Some of these are on islands.
- **Validation:** instead of every city being reachable by land (SPEC §3.9), every city and site must be reachable from both homes by land and sea, and each home's landmass must have a port (§6).
- **Start:** each player gets `I, I, S` and a wagon on their home city, and `start_goods` in it.
- **The default game's maps don't change.** The new generator parameters default to today's behavior.

## 4. Goods

### Sites

- **Production:** at the start of its owner's turn, a site adds `site_output` goods to its pile, up to `site_cap`. Neutral sites produce nothing.
- **Occupied sites:** a site neither produces nor delivers while an enemy unit stands on it, so a raider can cut off a site without capturing it.
- **Capture:** sites are captured like cities (SPEC §3.5), with the `C` order and `capture_points`. The pile goes with the site, so taking a site its owner has left alone for a while takes everything on it.
- **Vision:** a site gives its owner vision like a city (`city_vision`).
- **Next to a city:** right after production, a site next to one of its owner's cities moves its whole pile into that city. If there are several such cities, the lowest city id gets it. This is what it means for a city to have its own supply.

### Cities

- **Stockpile:** each city stores any amount of goods.
- **Building** pays the unit's cost from that city's stockpile. Otherwise it works as in SPEC §3.5.
- **Capture:** whoever captures a city takes its stockpile.
- **No gold:** there's no shared pool, city income, or gold upkeep. Neutral cities start empty.

### Wagons

- **Capacity:** a wagon carries up to `cargo_max` goods, as much as a full site holds.
- **Loading:** `L <wagon>` takes goods from the site or city the wagon stands on, which its player must hold, as many as fit.
- **Unloading:** `U <wagon>` puts the wagon's goods into the city it stands on, which its player must hold.
- **Amounts:** both orders take an optional amount, to move at most that many, for instance to leave some goods in a city.
- **Uses the action:** loading and unloading each use the wagon's action, like capturing. A wagon can move and then load or unload in the same turn, but can't move afterward.
  - A wagon passing through a city delivers nothing.
  - Even a site next to a city would take two turns per round trip, which is why such sites deliver by themselves.
- **Losing a wagon loses its cargo.** The goods aren't dropped or captured.
- **Feeding:** wagons also feed the units next to them (§5).

## 5. Food and sieges

**Rations.** Every unit carries up to `ration_max` rations. New units start full.

**Upkeep** at the start of each turn runs in this order, in place of income (SPEC §3.4):

1. **Production:** sites produce, and sites next to cities deliver (§4).
2. **Eating:** each of the player's units eats a ration. A unit that has none starves instead: it loses `starve_hp` HP, and can die of it.
3. **Refilling:** each unit refills to `ration_max` from one of its player's cities or wagons that is on its hex or next to it, paying `ration_cost` goods per ration.
   - Units are served in id order.
   - Each takes from the first source with goods: cities before wagons, and lower ids first.
   - A source that runs short pays for what it can.
4. **Healing,** as in SPEC §3.4, except that a unit that starved this turn doesn't heal. Ships heal next to their own cities (§6).
5. **Capture reset, refresh, and vision,** as in SPEC §3.4. Capture reset covers sites too.

**What it costs.** A unit costs `ration_cost` goods a turn wherever it is, paid when it refills. So a player's goods income limits the army they can keep, as gold upkeep does in the default game.

**Sieges** come out of these rules. With the starting values:

- Say a city holds 300 goods and 4 units. Their refills cost 20 goods a turn, so if no wagon gets in, the stockpile lasts 15 turns.
- Then the garrison lives on its rations for 10 turns, and then starves, at 10 HP a turn. Starving units also hit weaker, since damage scales with HP.
- A city with a site next to it takes in 50 goods a turn, more than a full garrison eats. To starve it, the besieger has to stand on the site or capture it.
- Besiegers eat too. A siege longer than 10 turns needs wagons behind the lines, which a relief force can raid.

**In the field,** an army needs wagons behind it or cities along the way, so how far it can go depends on its supply.

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
- **Fleets eat too.** One that stays away from port for more than 10 turns needs a wagon aboard a transport nearby.
- **From land,** only archers can hurt ships (§7).

## 7. Unit types

The five land units of the default game are unchanged (SPEC §3.3). The ruleset adds three:

| code | unit | cost | move | vision | range | flags |
|---|---|---|---|---|---|---|
| `W` | wagon | 100 | 4 | 2 | – | carry |
| `T` | transport | 150 | 6 | 2 | – | naval, transport |
| `G` | galley | 200 | 5 | 3 | 1 | naval, zoc |

**New flags:**

- `carry`: holds goods, and can load and unload them (§4).
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

- **Wagons** are easy prey, for cavalry most of all. That gives cavalry a strategic job: raiding supply.
- **Transports and galleys** can be hurt only by galleys and archers.
- **Bombardment,** the galley row's first five entries, is the riskiest guess. No land unit strikes back at a galley, and only archers can shoot at one on their own turn, so bombardment could make coastal cities too hard to hold. Setting those entries to 0 turns it off.

## 8. Fog of war

Fog works as in SPEC §4. For what's new:

- **Units:** a visible enemy unit shows everything, as now, including its cargo and rations. A visible transport shows the units aboard.
- **Cities and sites:** who owns them is public. A stockpile or pile shows only while you can see the city or site, as capture progress does (SPEC §3.6).

## 9. Winning

- **Out of cities** still loses (SPEC §3.8). Sites don't count.
- **Round limit:** 400 rounds on the 80 × 60 map. The tiebreak counts cities, then sites, then army value.
- **Elimination may take too long** on a big map. §13 lists other ways to win.

## 10. Rule constants

New keys, with their values in the strategic preset and in the default game:

| key | strategic | default | meaning |
|---|---|---|---|
| `supply` | 1 | 0 | 1 turns on goods and rations (§4, §5), in place of gold |
| `sites` | 32 | 0 | resource sites on generated maps, including the two next to the homes; must be even |
| `water_pct` | 40 | 6 | how much of a generated map is water |
| `start_goods` | 300 | – | goods in each home city at the start |
| `site_output` | 50 | – | goods a site produces per turn |
| `site_cap` | 1000 | – | the most goods a site's pile holds: 20 turns of output |
| `cargo_max` | 1000 | – | the most goods a wagon carries |
| `passengers_max` | 4 | – | the most units a transport carries |
| `ration_max` | 10 | – | the most rations a unit carries |
| `ration_cost` | 5 | – | goods per ration refilled |
| `starve_hp` | 10 | – | HP a starving unit loses per turn |

A dash means the key isn't used. The preset also sets the map to 80 × 60 with 46 neutral cities and the round limit to 400, and adds the site terrain, the naval move cost, and the three unit types.

## 11. Protocol changes

The additions follow the pattern fortification set: new fields go at the end of existing lines, and kits skip kinds of lines they don't know. Kits read a missing field as 0. A bot still needs new code to play this ruleset well.

**Init:**

```
> TERRAIN ~ water 0 0 0 0 1            ... conceal naval_move_cost (0 = ships can't enter)
> TERRAIN R site 1 0 0 0 0
> UNITTYPE W wagon 100 100 4 2 1 1 carry
> SITE 0 15 7 1                        id q r initial_owner, one per site
> RULE supply 1
```

**Turn state:**

```
> CITY 4 1 0 0 420                     id owner capturer progress goods
> SITE 0 1 0 0 650                     id owner capturer progress pile
> UNIT 41 1 W 12 9 100 0 0 0 10        id owner type q r hp fort carrier cargo rations
> LOG SITE 5 2 1                       a site changed hands: id, old owner, new owner
```

- There's no `GOLD` line.
- `carrier` is the transport a unit is aboard, or 0. It's the field SPEC §12 reserved for transport. A unit aboard reports its transport's hex.
- `SEE` lines get the same new fields as `UNIT` lines.

**Orders:**

```
< L <unit> [amount]         load goods from the site or city the wagon stands on
< U <unit> [amount]         unload goods into the city the wagon stands on
> OK <cargo> <goods>        the wagon's cargo afterward, and the goods left at the site or city
```

- `M` also boards and lands (§6), and `C` also captures sites.
- `B` pays from the city's stockpile, and puts ships next to the port.
- New error codes include `no_goods`, for a build the city's stockpile can't pay for, and one for attacks whose base damage is 0. Golden test vectors will pin the cases down, as they do for the default rules.

**Example.** A wagon at (12,9) drives onto site 0 at (15,7) and loads its pile. Then a pikeman at (10,8) boards transport 60, which waits offshore at (9,8):

```
< M 41 0012
> OK 15 7 0
< L 41
> OK 650 0
< M 52 3
> OK 9 8 0
```

## 12. Building it

### Phases

Each phase is playable on its own, and gets tournaments before the next one starts.

1. **Goods and sieges, on land.** Sites, stockpiles, wagons, the `L` and `U` orders, rations, and site placement, on the large map with today's water. The tournaments should answer:
   - Does a bot that hauls goods beat one that ignores them?
   - Do cities with sites next to them hold, and do cities without them fall to sieges?
   - How many orders a turn does supply take?
   - What should `site_output`, `site_cap`, `cargo_max`, and `ration_max` be?
2. **Ships.** The naval move cost, ports, transports, galleys, boarding and landing, and maps with more water. The tournaments should answer:
   - Can a player invade an island?
   - Do blockades work?
   - Is bombardment too strong?
3. **The rest.** A way to win on big maps, the viewer and play page, time controls, and a small strategic preset for play by hand (SPEC §5).

### Work

- **Rules:** a config can change the built-in unit types and terrains but can't add new ones. It needs to, so that `configs/strategic.toml` can add the site terrain and the three unit types.
- **Switch:** `supply` turns on goods and rations. Ships need no switch: they're unit types with the `naval` flag, and the default game has none.
- **State:** goods per city, a pile per site, and each unit's cargo, rations, and carrier.
- **Maps:** `SITE` lines in map files, and the generator's water, site placement, and land-and-sea check.
- **Determinism:** deliveries and refills go in id order, so every engine and kit agrees on them.
- **Kits:** the new lines and fields, pathfinding for ships, and helpers for loading and feeding.
- **Golden test vectors** for every new order, upkeep step, and edge case.
- **Viewer and play page:** sites, piles, stockpiles, cargo, passengers, and ships, plus boarding and landing by clicking.
- **A reference bot.** None of today's bots know about goods.
- **The benchmark:** a separate leaderboard. The ruleset also suits mode 3 (SPEC §5). Its big decisions, like which sites to take or where to land, play out over many turns, which is where a supervising LLM could help.

## 13. Open questions

1. **Rations, or food for garrisons only?** Feeding only the units on cities is simpler. But units outside cities would then cost nothing, so armies would grow without limit, and besiegers would need no supply. *Proposed: rations.*
2. **Should cities produce goods too?** A small trickle would make cities without sites worth more. But it would let every city feed a small garrison forever, which weakens sieges. *Proposed: no, to start.*
3. **A faster way to win on big maps?** Elimination could take hundreds of rounds. Options: you lose when you lose your home city, you win by holding most of the cities, or victory points decide at the round limit. *No proposal yet. Phase 1 games will show how long elimination takes.*
4. **Hidden passengers?** Showing what a transport carries is simpler. Hiding it would allow decoys. *Proposed: shown, to start.*
5. **Bombardment:** keep the galley's damage against land units, or set it to 0? *Proposed: keep it, and test it.*
6. **More warship types?** One type may make naval play flat. A fast raider and a heavy ship are the obvious pair. *Proposed: one, until phase 2 shows whether it's enough.*
7. **One kind of goods, or several?** Several, such as food, timber, and iron, would let unit types need different goods, but there would be more to haul and track. *Proposed: one.*
8. **Seizing wagons:** could an attacker take a wagon's cargo instead of destroying it? *Proposed: no, to start.*
9. **Roads:** terrain that makes wagons faster, which players build, so supply routes become something they make. SPEC §12 already has the hook for changing terrain (`TILE` lines). *Later.*

## 14. Agreed so far

Settled in discussion on 2026-11-21:

- **A separate ruleset.** The default game stays as it is.
- **Supply is carried.** Goods travel in wagons and transports.
  - *Rejected:* "connected" supply, where a site's goods would reach a city by themselves whenever a path between them avoided enemy units. Nobody would see the route, players would have to learn what blocks it, and the blocking rule would be easy for kits to get subtly wrong.
- **Few carriers that hold a lot,** and sites that store goods for many turns, up to a high cap.
- **Each city keeps its own stockpile.** With one shared pool, a delivery to any city would feed every city, and a siege would only cost some income.
- **Loading and unloading are orders,** like capturing, so a wagon can pass through a city without unloading.
- **Losing a carrier loses its cargo,** which makes escorts worth paying for.
- **Sites are captured like cities,** and the pile goes with the site.
- **Wagons ride on transports.** They're separate units, and neither is amphibious.
  - *Rejected:* an amphibious wagon. It would be odd next to infantry that can't swim, and it would let supply cross water without ships.
  - *Dropped:* transports holding goods themselves. Where there's no port, goods would need an extra order to pass from a wagon to a ship.
