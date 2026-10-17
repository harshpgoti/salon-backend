"""Fail closed unless DATABASE_URL reaches PostgreSQL 17 with an RLS runtime role."""
import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("BLOCKED: DATABASE_URL is unset; integration tests must not silently skip.")
        return 2
    engine = None
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            version, privileged = conn.execute(text(
                "SELECT current_setting('server_version_num')::int, "
                "(rolsuper OR rolbypassrls) FROM pg_roles WHERE rolname=current_user"
            )).one()
            if not 170000 <= version < 180000 or privileged:
                print("BLOCKED: tests require PostgreSQL 17 and a non-owner runtime role without BYPASSRLS.")
                return 2
            owned = conn.execute(text(
                "SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace "
                "AND relkind IN ('r','p') AND relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)"
            )).scalar_one()
            if owned:
                print("BLOCKED: runtime role must not own application tables.")
                return 2
            required = conn.execute(text(
                "SELECT to_regclass('tenants'), to_regclass('appointments'), to_regclass('communications')"
            )).one()
            if any(table is None for table in required):
                print("BLOCKED: schema is incomplete; apply migrations to a synthetic database first.")
                return 2
    except (SQLAlchemyError, ValueError, TypeError):
        print("BLOCKED: PostgreSQL preflight failed. No credentials or connection details logged.")
        return 2
    finally:
        if engine is not None:
            engine.dispose()
    print("PostgreSQL 17 runtime-role preflight passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
