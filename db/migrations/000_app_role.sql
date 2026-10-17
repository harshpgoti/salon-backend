-- Local Docker bootstrap; production provisions its runtime role separately.
CREATE ROLE salon_app LOGIN PASSWORD 'local-dev-only';
DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO salon_app', current_database()); END $$;
