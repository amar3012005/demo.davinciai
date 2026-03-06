"""
Fast Embeddings - Pre-cached ONNX model for quick startup
Loads model from local cache only (no download)
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

class FastEmbeddings:
    """Fast embedding wrapper using remote microservice"""
    
    def __init__(self, model_name="Xenova/all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.dimension = 384
        self._remote = None
        
        logger.info(f"🚀 Initializing remote bindings for embeddings via microservice...")
        try:
            from remote_embeddings import RemoteEmbeddings
            self._remote = RemoteEmbeddings()
            # Hardcoded to 384 to prevent pinging the service synchronously during app bootup
            self.dimension = 384
            logger.info(f"✅ Remote bindings ready ({self.dimension}-D)")
        except Exception as e:
            logger.error(f"Failed to initialize remote embeddings: {e}")
            self._remote = None
            self.dimension = 384
    
    def encode(self, texts, batch_size=32, show_progress_bar=False):
        if self._remote is None:
            logger.warning("⚠️ No model loaded, returning zeros")
            if isinstance(texts, str):
                return [0.0] * self.dimension
            return [[0.0] * self.dimension] * len(texts)
        
        try:
            embeddings = self._remote.encode(texts)
            if isinstance(texts, str):
                return embeddings.tolist()
            return embeddings.tolist()
        except Exception as e:
            logger.error(f"❌ Encode failed: {e}")
            if isinstance(texts, str):
                return [0.0] * self.dimension
            return [[0.0] * self.dimension] * len(texts)
    
    def __call__(self, texts, **kwargs):
        return self.encode(texts, **kwargs)

# Quick test
if __name__ == "__main__":
    import time
    
    print("⏱️ Testing fast embeddings...")
    start = time.time()
    
    emb = FastEmbeddings()
    
    elapsed = time.time() - start
    print(f"✅ Loaded in {elapsed:.2f}s")
    
    test_text = "Hello world"
    start = time.time()
    result = emb.encode(test_text)
    elapsed = time.time() - start
    
    print(f"✅ Encoded in {elapsed:.3f}s, dim={len(result)}")
