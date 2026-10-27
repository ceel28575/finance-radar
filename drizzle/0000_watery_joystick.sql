CREATE TABLE `radar_items` (
	`id` text PRIMARY KEY NOT NULL,
	`day` text NOT NULL,
	`room` text NOT NULL,
	`published_at` text NOT NULL,
	`payload` text NOT NULL,
	`received_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `items_day_time` ON `radar_items` (`day`,`published_at`);--> statement-breakpoint
CREATE TABLE `radar_reports` (
	`id` text PRIMARY KEY NOT NULL,
	`day` text NOT NULL,
	`generated_at` text NOT NULL,
	`payload` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `reports_day_time` ON `radar_reports` (`day`,`generated_at`);--> statement-breakpoint
CREATE TABLE `radar_state` (
	`key` text PRIMARY KEY NOT NULL,
	`value` text NOT NULL
);
