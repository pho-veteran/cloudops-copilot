"""Register feature persistence at process composition boundaries."""

from cloudops.features.access import models as access  # noqa: F401
from cloudops.features.incidents import models as incidents  # noqa: F401
from cloudops.features.investigations import models as investigations  # noqa: F401
from cloudops.features.workloads import models as workloads  # noqa: F401
from cloudops.shared import jobs  # noqa: F401
