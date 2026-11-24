import { index, integer, primaryKey, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const games = sqliteTable("games", {
  id: text("id").primaryKey(),
  playerName: text("player_name"),
  opponent: text("opponent").notNull(),
  mapName: text("map_name").notNull(),
  humanSeat: integer("human_seat").notNull(),
  startedAt: integer("started_at").notNull(),
  updatedAt: integer("updated_at").notNull(),
  round: integer("round").notNull().default(0),
  header: text("header").notNull(),
  outcome: text("outcome"),
  result: text("result"),
}, (g) => [index("games_recent").on(g.updatedAt)]);

export const turns = sqliteTable("turns", {
  gameId: text("game_id").notNull().references(() => games.id),
  seq: integer("seq").notNull(),
  revision: integer("revision").notNull(),
  data: text("data").notNull(),
}, (t) => [primaryKey({ columns: [t.gameId, t.seq] })]);
