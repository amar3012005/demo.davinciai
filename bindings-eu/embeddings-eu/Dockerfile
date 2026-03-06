FROM python:3.11-slim

# Install system dependencies needed for torch/sentence-transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pre-install larger dependencies separately to cache layers efficiently
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the default model into the image
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY app.py .

EXPOSE 4006

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4006"]
