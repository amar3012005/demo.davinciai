FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-optimized PyTorch first
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install other requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the model into the image to speed up first start
# trust_remote_code=True is required, and device='cpu' avoids meta tensor issues
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('jinaai/jina-clip-v1', trust_remote_code=True, device='cpu')"

COPY app.py .

# Use 4007 as default internal port to distinguish from standard embeddings
EXPOSE 4007

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4007"]
