from uuid import UUID, uuid4

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cloudops.shared.database import Base


class Workload(Base):
    __tablename__ = "workloads"
    __table_args__ = (UniqueConstraint("cluster", "namespace", "name", "uid"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    cluster: Mapped[str] = mapped_column(String(100))
    namespace: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    uid: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(default=True)
