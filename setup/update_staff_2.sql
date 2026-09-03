ALTER TABLE staff ADD COLUMN IF NOT EXISTS active_from date default null;
ALTER TABLE staff ADD COLUMN IF NOT EXISTS active_to   date default null;
