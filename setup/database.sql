-- psql -h localhost -u planning -d planning

CREATE TABLE staff (
	shortname varchar(100) not null primary key,
	hours_per_week numeric(5,2) not null,
	hours_per_day numeric(5,2) not null,
	remark varchar(500),
	is_active boolean default true
);

CREATE TYPE roleType AS ENUM ('Developer', 'Tester', 'Other');

CREATE TABLE roles (
	role_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	shortname varchar(100) not null REFERENCES staff(shortname) ON UPDATE CASCADE ON DELETE RESTRICT,
	role roleType not null
);

CREATE TYPE absenceType AS ENUM ('Urlaub', 'Krank', 'GLAZ', 'Other');

CREATE TABLE absence (
	absence_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	shortname varchar(100) not null REFERENCES staff(shortname) ON UPDATE CASCADE ON DELETE RESTRICT,
	absence_from date not null,
	absence_to date not null,
	absence_type absenceType not null
);


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

create table worked_hours (
	worked_hours_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	project_id int REFERENCES project(project_id),
	day date not null,
	impl_hours int default 0,
	test_hours int default 0
);

create table planning(
	task_id int default null REFERENCES tasks(task_id),
	project_id int default null REFERENCES project(project_id),
	staff varchar(100) not null REFERENCES staff(shortname) ON UPDATE CASCADE ON DELETE RESTRICT,
	role_id int not null REFERENCES roles(role_id),
	start_date date not null,
	end_date date not null
);
