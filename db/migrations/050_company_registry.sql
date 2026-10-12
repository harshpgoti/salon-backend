-- TASK-050 / FR-070, FR-076: company staff and tenant registry.
--
-- Company operators are a separate identity surface from salon staff
-- (separate sessions, separate roles, separate application). These tables
-- are company-global — no tenant scope — and every access is role-gated:
--   company_staff - employee identity link (issuer+subject), name, role,
--                   MFA enrollment flag and active switch. Roles: admin
--                   (all), finance (money surfaces, TASK-052), support
--                   (cases and grants, TASK-051), success (registry and
--                   onboarding health). Deactivation revokes company sessions.
--
-- The tenant registry itself is a read model assembled from tenant-owned
-- rows (plan, lifecycle, onboarding, channel setup, health signals). It
-- carries aggregates only — never client names, contacts, notes or message
-- content — enforced by construction (the query selects no such column).

CREATE TABLE company_staff (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    issuer text NOT NULL,
    subject text NOT NULL,
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 120),
    -- admin   = people and everything
    -- finance = money movement and reconciliation (TASK-052), never client notes
    -- support = cases and access grants (TASK-051)
    -- success = registry, onboarding and renewal health (TASK-062)
    role text NOT NULL CHECK (role IN ('admin', 'finance', 'support', 'success')),
    -- Synthetic stand-in for the OIDC amr=mfa evidence the company login
    -- requires (D-011): unenrolled staff cannot use company sessions.
    mfa_enrolled boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (id),
    UNIQUE (issuer, subject),
    FOREIGN KEY (issuer, subject) REFERENCES identity_users(issuer, subject) ON DELETE CASCADE
);

-- Company-global by design (the registry's job is cross-tenant overview);
-- every route enforces a company role instead of a tenant scope.
GRANT SELECT, INSERT, UPDATE ON company_staff TO salon_app;

-- Privileged-action audit (FR-076): staff invites, deactivations, suspensions
-- and finance operations record who did what to whom, outside any tenant's
-- own audit trail (a tenant must never see company-internal actions).
CREATE TABLE company_audit_events (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    actor_issuer text NOT NULL,
    actor_subject text NOT NULL,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id text NOT NULL,
    reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

GRANT SELECT, INSERT ON company_audit_events TO salon_app;
