from alembic import context

from cloudops.entrypoints import metadata  # noqa: F401
from cloudops.shared.config import settings
from cloudops.shared.database import Base, engine

config = context.config


def run():
    with engine().connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=settings().database_url, target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run()
