FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    dnsutils \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt ./requirements.txt

# Test network connectivity before pip install
RUN curl -I https://pypi.org/ || (echo "Network test failed" && exit 1)

# Install Python dependencies with retry and timeout
RUN pip install --no-cache-dir --timeout=300 --retries=10 -r requirements.txt

# Copy application code
COPY . .

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 8004

# Run application
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8004"]
