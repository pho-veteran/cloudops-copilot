from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=256)


class Principal(BaseModel):
    id: UUID
    username: str
    role: Literal["viewer", "operator"]


class SessionView(BaseModel):
    principal: Principal
    csrf_token: str
    expires_at: datetime
