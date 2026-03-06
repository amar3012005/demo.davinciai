import logging
import os
from typing import List, Union
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Embeddings Microservice")

model_name = os.getenv("EMBEDDINGS_MODEL", "all-MiniLM-L6-v2")
logger.info(f"Loading SentenceTransformer model: {model_name}")
model = SentenceTransformer(model_name)
logger.info("Model loaded successfully.")

class EmbeddingsRequest(BaseModel):
    sentences: Union[str, List[str]]

@app.post("/embed")
def embed_sentences(request: EmbeddingsRequest):
    try:
        sentences = request.sentences
        if isinstance(sentences, str):
            sentences = [sentences]
            
        # encode returns numpy array by default
        embeddings = model.encode(sentences)
        return {"embeddings": embeddings.tolist()}
    except Exception as e:
        logger.error(f"Error encoding sentences: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "ok", "model": model_name}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "4006"))
    uvicorn.run(app, host="0.0.0.0", port=port)
