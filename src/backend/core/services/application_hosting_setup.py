"""Provision and upgrade only the dedicated application database."""
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from core.services.application_store import application_engine, initialize_store

def provision_owner(url):
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or parsed.username != "application_admin":
        raise RuntimeError("Provisioning requires the dedicated application_admin connection")
    from core.config.application_hosting import application_hosting_settings
    from core.db.engine import DATABASE_URL
    target = make_url(application_hosting_settings.database_url)
    platform = make_url(DATABASE_URL)
    identity = lambda value: (value.get_backend_name(), value.host, value.port or 5432, value.database)
    if identity(parsed) != identity(target) or identity(parsed) == identity(platform):
        raise RuntimeError("Provisioning must target the configured separate application database")
    if not target.password or target.password == parsed.password:
        raise RuntimeError("Owner and bootstrap credentials must differ")
    if target.username != "application_owner":
        raise RuntimeError("Application runtime must use application_owner")
    engine = create_engine(url, echo=False, hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('hugagent.owner_password', :value, true)"),
                               {"value": target.password})
            connection.execute(text("""
                DO $owner$ BEGIN
                    IF EXISTS(SELECT FROM pg_roles WHERE rolname='application_owner') THEN
                        EXECUTE format('ALTER ROLE application_owner LOGIN NOSUPERUSER NOCREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
                                       current_setting('hugagent.owner_password'));
                    ELSE
                        EXECUTE format('CREATE ROLE application_owner LOGIN NOSUPERUSER NOCREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
                                       current_setting('hugagent.owner_password'));
                    END IF;
                END $owner$
            """))
            # Bootstrap superusers also own system objects; transfer only application objects.
            quote = connection.dialect.identifier_preparer.quote
            schemas = connection.scalars(text("""
                SELECT nspname FROM pg_namespace
                WHERE nspowner=(SELECT oid FROM pg_roles WHERE rolname='application_admin')
                  AND nspname ~ '^app_[0-9a-f]{32}$'
            """)).all()
            for schema in schemas:
                connection.exec_driver_sql(f"ALTER SCHEMA {quote(schema)} OWNER TO application_owner")
            objects = connection.execute(text("""
                SELECT n.nspname,c.relname,c.relkind FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE c.relowner=(SELECT oid FROM pg_roles WHERE rolname='application_admin')
                  AND (n.nspname='public' OR n.nspname ~ '^app_[0-9a-f]{32}$')
                  AND c.relkind IN ('r','p','S','v','m','f')
            """)).all()
            kinds = {'r': 'TABLE', 'p': 'TABLE', 'S': 'SEQUENCE',
                     'v': 'VIEW', 'm': 'MATERIALIZED VIEW', 'f': 'FOREIGN TABLE'}
            for schema, name, kind in objects:
                connection.exec_driver_sql(
                    f"ALTER {kinds[kind]} {quote(schema)}.{quote(name)} OWNER TO application_owner")
            connection.exec_driver_sql(
                f"GRANT CREATE ON DATABASE {quote(parsed.database)} TO application_owner")
            connection.exec_driver_sql("GRANT USAGE, CREATE ON SCHEMA public TO application_owner")
            roles = connection.scalars(text("SELECT rolname FROM pg_roles WHERE rolname ~ '^app_role_[0-9a-f]{32}$'"))
            for role in roles:
                connection.exec_driver_sql(f'GRANT "{role}" TO application_owner WITH ADMIN OPTION')
    except Exception:
        raise RuntimeError("Dedicated application owner provisioning failed; check privileges and configuration") from None
    finally:
        engine.dispose()

def main():
    url = os.getenv("APPLICATION_PROVISION_DATABASE_URL", "").strip()
    if url:
        provision_owner(url)
    engine = application_engine()
    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            if connection.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")):
                raise RuntimeError("Application runtime must use a non-superuser owner role")
    initialize_store(engine)
    print("Application database registry and recovery history provisioned")

if __name__ == "__main__":
    main()
