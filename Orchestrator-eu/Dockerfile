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

# Upgrade packaging tooling first (improves resolver/wheel compatibility on CI builders)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Install Python dependencies with retry, timeout, and binary preference
RUN pip install --no-cache-dir --prefer-binary --timeout=300 --retries=10 -r requirements.txt

# Copy application code
COPY . .

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 8004

# Run application
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8004"]
