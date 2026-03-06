import os
import logging
import httpx
import numpy as np
from typing import List, Union

logger = logging.getLogger(__name__)

class RemoteEmbeddings:
    def __init__(self, endpoint_url: str = None):
        if endpoint_url is None:
            endpoint_url = os.getenv("EMBEDDINGS_SERVICE_URL", "http://embeddings-eu:4006/embed")
        self.endpoint_url = endpoint_url
        
    def encode(self, sentences: Union[str, List[str]], **kwargs) -> np.ndarray:
        if isinstance(sentences, str):
            sentences = [sentences]
            
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(self.endpoint_url, json={"sentences": sentences})
                response.raise_for_status()
                data = response.json()
                embeddings_list = data.get("embeddings", [])
                return np.array(embeddings_list)
        except Exception as e:
            logger.error(f"Failed to get remote embeddings: {e}")
            # return zero vector as fallback to avoid crashing (same length as MiniLM, usually 384)
            return np.zeros((len(sentences), 384))
