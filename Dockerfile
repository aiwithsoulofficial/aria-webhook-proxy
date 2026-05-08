FROM python:3.11-slim

# Install system deps for Playwright Chromium
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

CMD gunicorn main:app --bind 0.0.0.0:${PORT:-3000} --timeout 120 --workers 2
