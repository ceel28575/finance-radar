import { sqliteTable, text, index } from 'drizzle-orm/sqlite-core';
export const items = sqliteTable('radar_items', {
 id:text('id').primaryKey(), day:text('day').notNull(), room:text('room').notNull(),
 publishedAt:text('published_at').notNull(), payload:text('payload').notNull(), receivedAt:text('received_at').notNull(),
}, t=>[index('items_day_time').on(t.day,t.publishedAt)]);
export const reports = sqliteTable('radar_reports', {
 id:text('id').primaryKey(), day:text('day').notNull(), generatedAt:text('generated_at').notNull(), payload:text('payload').notNull(),
},t=>[index('reports_day_time').on(t.day,t.generatedAt)]);
export const state=sqliteTable('radar_state',{key:text('key').primaryKey(),value:text('value').notNull()});
