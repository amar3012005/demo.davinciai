import logging
import os
from typing import List, Union, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
import numpy as np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Jina CLIP v1 Microservice")

# Model name from environment or default
model_name = os.getenv("MODEL_NAME", "jinaai/jina-clip-v1")

logger.info(f"Loading Jina CLIP model: {model_name}")
# trust_remote_code=True is essential for Jina CLIP v1
# forcing device='cpu' to avoid "meta tensor" issues during initialization in some environments
model = SentenceTransformer(model_name, trust_remote_code=True, device='cpu')
logger.info("Model loaded successfully.")

class TextEmbedRequest(BaseModel):
    sentences: Union[str, List[str]]

class ImageEmbedRequest(BaseModel):
    # Can be URLs, file paths, or base64 (if handled in encode)
    image_inputs: Union[str, List[str]]

@app.post("/embed/text")
def embed_text(request: TextEmbedRequest):
    """
    Endpoint for text embeddings.
    """
    try:
        sentences = request.sentences
        if isinstance(sentences, str):
            sentences = [sentences]
            
        logger.info(f"Encoding {len(sentences)} text inputs")
        embeddings = model.encode(sentences)
        return {"embeddings": embeddings.tolist()}
    except Exception as e:
        logger.error(f"Error encoding text: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/embed/image")
def embed_image(request: ImageEmbedRequest):
    """
    Endpoint for image embeddings. Supports URLs and local paths.
    """
    try:
        inputs = request.image_inputs
        if isinstance(inputs, str):
            inputs = [inputs]
            
        logger.info(f"Encoding {len(inputs)} image inputs")
        # Jina CLIP v1 model.encode handles image URLs and PIL images
        embeddings = model.encode(inputs)
        return {"embeddings": embeddings.tolist()}
    except Exception as e:
        logger.error(f"Error encoding images: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/compare")
def compare(text: str, image_url: str):
    """
    Helper endpoint to compare text and image similarity.
    """
    try:
        text_emb = model.encode([text])[0]
        img_emb = model.encode([image_url])[0]
        
        # Cosine similarity
        similarity = np.dot(text_emb, img_emb) / (np.linalg.norm(text_emb) * np.linalg.norm(img_emb))
        return {"similarity": float(similarity)}
    except Exception as e:
        logger.error(f"Error during comparison: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "ok", "model": model_name}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "4007"))
    uvicorn.run(app, host="0.0.0.0", port=port)
