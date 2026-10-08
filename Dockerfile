# Pinned base image (python:3.12-slim-<debian release> with tag and digest)
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create non-root user and group with explicit numeric IDs
RUN groupadd -r -g 999 appuser && useradd -r -u 999 -g appuser -d /app -s /sbin/nologin appuser

# Dependency layer caching: install requirements first
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY --chown=appuser:appuser app/ ./app/

# Switch to non-root user
USER 999:999

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
