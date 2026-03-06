import os
import logging
import numpy as np
from typing import List, Union

logger = logging.getLogger(__name__)

class OptimizedEmbeddings:
    """
    High-performance embedding wrapper leveraging the remote microservice.
    Replaces ONNX/sentence-transformers for vCPU environments.
    """

    def __init__(self, model_path: str = "BAAI/bge-m3", model_name: str = "BAAI/bge-m3", device: str = "cpu"):
        self.model_name = model_name
        self.device = device
        
        logger.info(f"🚀 Initializing remote bindings for embeddings via microservice...")
        try:
            from remote_embeddings import RemoteEmbeddings
            self._remote = RemoteEmbeddings()
            # Hardcoded to 384 to avoid blocking ping during Docker compose up
            self.dimension = 384
            logger.info(f"✅ Remote bindings ready ({self.dimension}-D)")
        except Exception as e:
            logger.error(f"Failed to initialize remote embeddings: {e}")
            self._remote = None
            self.dimension = 384

    def encode(self, sentences: Union[str, List[str]], convert_to_numpy: bool = True, normalize_embeddings: bool = True) -> np.ndarray:
        if self._remote:
            res = self._remote.encode(sentences)
            return res
        # Fallback
        if isinstance(sentences, str):
            return np.zeros(self.dimension)
        return np.zeros((len(sentences), self.dimension))

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if self._remote:
            embeddings = self._remote.encode(texts)
            return embeddings.tolist()
        return np.zeros((len(texts), self.dimension)).tolist()

    def embed_query(self, text: str) -> List[float]:
        if self._remote:
            embedding = self._remote.encode([text])[0]
            return embedding.tolist()
        return np.zeros(self.dimension).tolist()
