FROM python:3.11-slim

# System deps: Chromium + Xvfb (needed for headless=False in container)
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    xvfb \
    x11-utils \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libgbm1 \
    libasound2 \
    fonts-liberation \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Tell Playwright / browser-use to use the system Chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/usr/bin
ENV CHROME_BIN=/usr/bin/chromium

COPY . .

# Create input/output mount points
RUN mkdir -p /data/input /data/output /app/jobs

# Xvfb display so the non-headless browser has somewhere to render
ENV DISPLAY=:99

EXPOSE 8000

# Start Xvfb then the server
CMD Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset & \
    sleep 1 && \
    python server.py
