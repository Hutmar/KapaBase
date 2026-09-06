ALTER TABLE staff DROP COLUMN IF EXISTS default_task_id;

CREATE TABLE default_task (
	default_task_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	task_id int REFERENCES tasks(task_id) ON DELETE CASCADE,
	shortname varchar(100) not null REFERENCES staff(shortname) ON UPDATE CASCADE ON DELETE CASCADE,
	active_from date default null,
	active_to date default null
);
