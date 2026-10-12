-- Local Docker bootstrap role. Production provisions its runtime role through the secrets manager.
CREATE ROLE salon_app LOGIN PASSWORD 'local-dev-only';
GRANT CONNECT ON DATABASE salon TO salon_app;
