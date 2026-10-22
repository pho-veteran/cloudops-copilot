FROM ghcr.io/astral-sh/uv:0.9.26 AS uv
FROM python:3.13.11-slim-bookworm
COPY --from=uv /uv /uvx /bin/
WORKDIR /workspace
COPY services/backend/pyproject.toml services/backend/uv.lock services/backend/
RUN uv sync --project services/backend --frozen --no-install-project
COPY services/backend services/backend
COPY config config
RUN uv sync --project services/backend --frozen
ENV PATH="/workspace/services/backend/.venv/bin:$PATH"
CMD ["uvicorn", "cloudops.entrypoints.api:app", "--host", "0.0.0.0", "--port", "8000"]
