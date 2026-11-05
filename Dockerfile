FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /srv
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./

FROM base AS prod
RUN uv sync --frozen --no-dev
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY mock_helpdesk ./mock_helpdesk
RUN useradd --system --no-create-home app
USER app

FROM base AS dev
RUN uv sync --frozen
COPY . .
