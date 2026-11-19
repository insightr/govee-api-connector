FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Groups and presets; mount your own over this to change them without rebuilding.
COPY config.yaml ./config.yaml

# Snapshots are written here; mount a volume to keep them across restarts.
VOLUME /app/data
EXPOSE 8000

# The API key is passed at run time (--env-file .env). It is never baked into the image.
CMD ["govee", "web", "--host", "0.0.0.0"]
