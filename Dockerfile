FROM python:3.14-slim

WORKDIR /app

# Install OS deps for psycopg (libpq)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Install package in editable mode so `app` is importable
RUN pip install --no-cache-dir -e .

# Make startup script executable
RUN chmod +x scripts/start.sh
RUN chmod +x scripts/start-worker.sh || true

EXPOSE 8010

CMD ["scripts/start.sh"]
