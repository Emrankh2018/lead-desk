from alembic import context

from leadgen import models  # noqa: F401  (registers tables on Base.metadata)
from leadgen.config import get_settings
from leadgen.db import Base, make_engine

config = context.config
# The app passes its own URL (e.g. a test database); otherwise use configured settings.
url = config.attributes.get("url") or get_settings().db_url


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = make_engine(url)
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
