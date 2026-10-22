from pydantic import BaseModel, ConfigDict


class View(BaseModel):
    model_config = ConfigDict(from_attributes=True)
