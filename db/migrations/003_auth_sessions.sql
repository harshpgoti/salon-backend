CREATE TABLE auth_sessions (
    session_hash char(64) PRIMARY KEY,
    surface text NOT NULL CHECK (surface IN ('staff','company')),
    issuer text NOT NULL,
    subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at)
);
CREATE INDEX auth_sessions_expiry_idx ON auth_sessions (expires_at);
GRANT SELECT, INSERT, DELETE ON auth_sessions TO salon_app;
