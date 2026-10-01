import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import Settings
from app.infrastructure.persistence.database import normalize_database_url

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# disable_existing_loggers=False is deliberate: the default (True) switches off
# every logger that already exists, so running a migration in-process -- from a
# test, or from an application startup hook -- would silence the application's own
# loggers for the rest of the process.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here
# for 'autogenerate' support
#
# Still None: there is no declarative metadata to point at yet, so
# `alembic revision --autogenerate` cannot diff the schema and revisions are
# written by hand. Defining the ORM models belongs to ticket #6; once they exist,
# set this to their Base.metadata.
target_metadata = None

# The placeholder URL that ships in alembic.ini is not usable, and keeping a real
# one in a committed file would be a committed credential (constraint C6). The
# connection is resolved from the application's own configuration instead -- the
# same DATABASE__URL that the running app uses -- so `alembic upgrade head` works
# from the environment with no extra setup.
_INI_PLACEHOLDER_PREFIX = "driver://"


def _database_url() -> str:
    """Resolve the database URL for this migration run.

    Precedence, most explicit first:

    1. ``alembic -x url=...`` on the command line, for a one-off target.
    2. ``sqlalchemy.url`` in alembic.ini, when it has been set to something real
       (the shipped placeholder does not count). This is also what a programmatic
       ``config.set_main_option`` sets, so a caller driving Alembic from code
       stays in control.
    3. ``DATABASE__URL`` from the application configuration, which is the normal
       path for every environment.

    A sync URL is rewritten to the async driver, so ``postgresql://...`` works
    here exactly as it does for the application.
    """
    from_cli = context.get_x_argument(as_dictionary=True).get("url", "").strip()
    if from_cli:
        return normalize_database_url(from_cli)

    from_ini = (config.get_main_option("sqlalchemy.url") or "").strip()
    if from_ini and not from_ini.startswith(_INI_PLACEHOLDER_PREFIX):
        return normalize_database_url(from_ini)

    # Settings() rather than get_settings(): the cached accessor could hand back a
    # value captured before this process set its environment.
    from_settings = (Settings().database.url or "").strip()
    if from_settings:
        return normalize_database_url(from_settings)

    raise RuntimeError(
        "No database URL configured for Alembic. Set DATABASE__URL in the "
        "environment (or .env), or pass `alembic -x url=postgresql://...`."
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=config.attributes.get("version_table_schema"),
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = dict(config.get_section(config.config_ini_section, {}) or {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


supplied_connection = config.attributes.get("connection")
if supplied_connection is not None:
    do_run_migrations(supplied_connection)
elif context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
