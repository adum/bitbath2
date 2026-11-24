CREATE TABLE `games` (
	`id` text PRIMARY KEY NOT NULL,
	`player_name` text,
	`opponent` text NOT NULL,
	`map_name` text NOT NULL,
	`human_seat` integer NOT NULL,
	`started_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	`round` integer DEFAULT 0 NOT NULL,
	`header` text NOT NULL,
	`outcome` text,
	`result` text
);
--> statement-breakpoint
CREATE INDEX `games_recent` ON `games` (`updated_at`);--> statement-breakpoint
CREATE TABLE `turns` (
	`game_id` text NOT NULL,
	`seq` integer NOT NULL,
	`revision` integer NOT NULL,
	`data` text NOT NULL,
	PRIMARY KEY(`game_id`, `seq`),
	FOREIGN KEY (`game_id`) REFERENCES `games`(`id`) ON UPDATE no action ON DELETE no action
);
