-- psql -h localhost -u planning -d planning

CREATE TYPE projectType AS ENUM ('Project', 'Operations', 'Internal');

create table project (
	project_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	project_name VARCHAR(200) NOT NULL,
	sort_order int default 0,
	customer VARCHAR(100) NOT NULL,
	jira_id varchar(100),
	target_hours int not null,
	impl_hours int not null,
	test_hours int not null,
	planned boolean default TRUE,
	start_date date,
	due_date date,
	remarks VARCHAR(1000) default null,
	done boolean default FALSE,
	color_hexcode char(7) default null,
	project_type projectType not null default 'Project', 
	unique(color_hexcode)
);

create table tasks (
	task_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	project_id int default null REFERENCES project(project_id),
	task_name varchar(200) NOT NULL,
	color_hexcode char(7) default null,
	unique(color_hexcode)
);

-- HINWEIS: Ursprünglich enthielt diese Tabellendefinition zwei Fehler
-- (";" statt "," nach default_task_id, sowie "active_to data" statt
-- "active_to date"), wodurch die Tabelle so nie erstellt werden konnte.
-- Beides wurde hier korrigiert. Die Felder active_from/active_to werden
-- ab jetzt aktiv genutzt (Mitarbeiterverwaltung, Planung, Abwesenheiten).
CREATE TABLE staff (
	shortname varchar(100) not null primary key,
	hours_per_week numeric(5,2) not null,
	hours_per_day numeric(5,2) not null,
	remark varchar(500),
	default_task_id int REFERENCES tasks(task_id) ON DELETE SET NULL,
	is_active boolean default true,
	active_from date default null,
	active_to date default null
);

CREATE TABLE default_task (
	default_task_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	task_id int REFERENCES tasks(task_id) ON DELETE CASCADE,
	shortname varchar(100) not null REFERENCES staff(shortname) ON UPDATE CASCADE ON DELETE CASCADE,
	active_from date default null,
	active_to date default null
);

CREATE TYPE roleType AS ENUM ('Developer', 'Tester', 'Other');

CREATE TABLE roles (
	role_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	shortname varchar(100) not null REFERENCES staff(shortname) ON UPDATE CASCADE ON DELETE RESTRICT,
	role roleType not null
);

CREATE TYPE absenceType AS ENUM ('Urlaub', 'Krank', 'GLAZ', 'Other', 'Teamday');

CREATE TABLE absence (
	absence_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	shortname varchar(100) not null REFERENCES staff(shortname) ON UPDATE CASCADE ON DELETE RESTRICT,
	absence_from date not null,
	absence_to date not null,
	absence_type absenceType not null
);

create table worked_hours (
	worked_hours_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	project_id int REFERENCES project(project_id),
	day date not null,
	impl_hours int default 0,
	test_hours int default 0
);

create table planning_variant (
	variant_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	created_at TIMESTAMPTZ DEFAULT NOW(),
	variant_name varchar(255) default null,
	is_active BOOLEAN NOT NULL DEFAULT false,
	active_since TIMESTAMPTZ default null
);

CREATE UNIQUE INDEX idx_nur_ein_aktiver_datensatz 
ON planning_variant (is_active) 
WHERE is_active = true;

create table planning(
	planning_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	task_id int default null REFERENCES tasks(task_id),
	project_id int default null REFERENCES project(project_id),
	staff varchar(100) not null REFERENCES staff(shortname) ON UPDATE CASCADE ON DELETE RESTRICT,
	role_id int not null REFERENCES roles(role_id),
	variant_id int not null REFERENCES planning_variant(variant_id),
	start_date date not null,
	end_date date not null,
	CONSTRAINT chk_task_xor_project CHECK (
        (task_id IS NULL) != (project_id IS NULL)
    ),
    CONSTRAINT uq_planning UNIQUE NULLS NOT DISTINCT (
        task_id, project_id, staff, role_id, variant_id, start_date, end_date
    )
);

create table milestone(
	milestone_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	project_id int not null REFERENCES project(project_id) ON DELETE CASCADE,
	milestone_name VARCHAR(200) NOT NULL,
	color_schema VARCHAR(50) NOT NULL,
	due_date date not null,
	unique(project_id, due_date)
);
