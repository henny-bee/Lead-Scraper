FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
# PLAN Q14: install dependencies only (the project source is not copied yet) …
RUN pip install uv && uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
# PLAN Q-E6: the industry catalog (data/isic/industries.yaml) is required at runtime.
COPY data ./data
# … then install the project itself (PLAN Q14).
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Optional healthcheck with the image's own Python (no curl); /health = ready (T24).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

# Single Uvicorn worker: job state lives in this process's memory (A§8, C15).
CMD ["uvicorn", "leadscraper.main:app", "--host", "0.0.0.0", "--port", "8000"]
