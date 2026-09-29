FROM python:3.12.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src/ ./src/
RUN uv sync --frozen --no-dev --no-editable
RUN useradd --system --uid 10001 app
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "signaldesk.main:app", "--host", "0.0.0.0", "--port", "8000"]
