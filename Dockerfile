# Reproducible runtime for the feedstock margin monitor.
# Build:  docker build -t feedstock-monitor .
# Daily:  docker run --rm -v "$PWD:/app" --env-file .env feedstock-monitor
# Dash:   docker run --rm -p 5000:5000 -v "$PWD:/app" --env-file .env \
#           feedstock-monitor feedstock-dashboard
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better layer caching) from the package metadata.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# config.json + the SQLite DB live at the project root; mount the repo at /app
# so the accumulating feedstock.db persists on the host.
COPY config.json ./

# Default command runs the full daily pipeline; override to launch the dashboard.
ENV FLASK_RUN_HOST=0.0.0.0
EXPOSE 5000
CMD ["feedstock-run"]
