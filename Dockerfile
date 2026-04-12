FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app && useradd -u ${APP_UID} -g ${APP_GID} -m -s /bin/bash app

WORKDIR /app
RUN chown app:app /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

USER app
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY --chown=app:app . .

CMD ["uv", "run", "python", "-m", "src.main"]
