FROM python:3.12-slim

# Install system deps needed by sqlite-vec (bundled native extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Database lives in a volume — don't bake it into the image
VOLUME ["/app/data"]

# Point DB_PATH at the volume (override at runtime via env var if needed)
ENV MEMORY_DB_PATH=/app/data/memory.db

EXPOSE 8900

# Run the HTTP API + admin UI + pattern engine
CMD ["python", "api.py"]
