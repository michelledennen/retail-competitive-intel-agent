# Container image for Google Cloud Run (or any container platform).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    # Persistent sessions via DatabaseSessionService. SQLite on local disk is
    # fine for a single instance; for multi-instance production set
    # SESSION_SERVICE_URI to Cloud SQL, e.g. postgresql+asyncpg://...
    SESSION_SERVICE_URI=sqlite+aiosqlite:////app/.adk/sessions.db \
    MEMORY_SERVICE_URI=sqlite:////app/.adk/memory.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY retail_intel_agent/ ./retail_intel_agent/

# Run as a non-root user that owns the local state directory.
RUN useradd --create-home appuser && mkdir -p /app/.adk && chown -R appuser /app/.adk
USER appuser

EXPOSE 8080

# Serves the ADK web UI + REST API. Cloud Run injects $PORT.
# No secrets are baked into the image: GOOGLE_API_KEY is injected at runtime
# from Secret Manager (see terraform/main.tf).
CMD ["sh", "-c", "adk web --host 0.0.0.0 --port ${PORT} --session_service_uri ${SESSION_SERVICE_URI} --memory_service_uri ${MEMORY_SERVICE_URI} ."]
