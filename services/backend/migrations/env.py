from alembic import context

# Composition root registers feature persistence for migration metadata.
from cloudops.features.access import models as access_models  # noqa: F401
from cloudops.features.incidents import models as incident_models  # noqa: F401
from cloudops.features.investigations import models as investigation_models  # noqa: F401
from cloudops.features.workloads import models as workload_models  # noqa: F401
from cloudops.shared import jobs  # noqa: F401
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
