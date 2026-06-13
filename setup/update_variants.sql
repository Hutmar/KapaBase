create table planning_variant (
	variant_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
	created_at TIMESTAMPTZ DEFAULT NOW(),
	variant_name varchar(255) default null,
	is_active BOOLEAN NOT NULL DEFAULT false,
	active_since TIMESTAMPTZ default null
);

CREATE UNIQUE INDEX idx_nur_ein_aktiver_datensatz ON planning_variant (is_active) WHERE is_active = true;

ALTER TABLE planning ADD COLUMN variant_id int REFERENCES planning_variant(variant_id);

INSERT INTO planning_variant(is_active, active_since) VALUES (TRUE, NOW());

UPDATE planning SET variant_id = 1 WHERE variant_id IS NULL;

ALTER TABLE planning ALTER COLUMN variant_id SET NOT NULL;

ALTER TABLE planning DROP CONSTRAINT IF EXISTS uq_planning;

ALTER TABLE planning ADD CONSTRAINT uq_planning UNIQUE NULLS NOT DISTINCT (
    task_id, project_id, staff, role_id, variant_id, start_date, end_date
);

