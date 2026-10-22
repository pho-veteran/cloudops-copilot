from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


async def domain_error_handler(request: Request, exc: DomainError):
    return JSONResponse(
        status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
    )


async def validation_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request fields are invalid. Check required values and bounds.",
            }
        },
    )


ERROR_RESPONSES = {n: {"model": ErrorEnvelope} for n in (401, 403, 404, 409, 422, 503)}
