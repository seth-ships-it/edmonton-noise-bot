FROM python:3.11-slim-bookworm

# Install audio drivers, compiler tools, and network tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libasound2-dev \
    libportaudio2 \
    portaudio19-dev \
    alsa-utils \
    network-manager \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV DASHBOARD_PORT=5000

# Expose web dashboard port
EXPOSE 5000

# Run supervisor (runs both audio detector and dashboard)
CMD ["python", "run.py"]
