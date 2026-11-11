CREATE TYPE dayOfWeek AS ENUM ('Mo', 'Di', 'Mi', 'Do', 'Fr');
CREATE TABLE planned_absence (
	planned_absence_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	absenceDay dayOfWeek NOT NULL, 
	shortname varchar(100) not null REFERENCES staff(shortname) ON UPDATE CASCADE ON DELETE RESTRICT,
	unique(shortname, absenceDay)
);