"""
Fast Embeddings - Pre-cached ONNX model for quick startup
Loads model from local cache only (no download)
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

class FastEmbeddings:
    """Fast embedding loader with pre-cached model"""
    
    def __init__(self, model_name="Xenova/all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = None
        self.dimension = 384
        
        # Try to load from cache only (no download)
        self._load_from_cache()
    
    def _load_from_cache(self):
        """Load model from local cache only"""
        try:
            # Check if model is already cached
            cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
            
            # Convert model name to cache folder name
            model_folder = f"models--{self.model_name.replace('/', '--')}"
            cache_path = cache_dir / model_folder
            
            if cache_path.exists():
                logger.info(f"✅ Found cached model: {model_folder}")
                self._load_model()
            else:
                logger.warning(f"⚠️ Model not cached, using fallback")
                self._use_fallback()
                
        except Exception as e:
            logger.error(f"❌ Cache load failed: {e}")
            self._use_fallback()
    
    def _load_model(self):
        """Load ONNX model from cache"""
        try:
            from optimum.onnxruntime import ORTModelForFeatureExtraction
            from transformers import AutoTokenizer
            
            logger.info("🚀 Loading cached ONNX model...")
            
            # Load from cache (no download)
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.model_name,
                local_files_only=True  # CRITICAL: No download
            )
            
            self.model = ORTModelForFeatureExtraction.from_pretrained(
                self.model_name,
                from_transformers=True,
                local_files_only=True  # CRITICAL: No download
            )
            
            logger.info(f"✅ Cached ONNX model loaded (384-D)")
            
        except Exception as e:
            logger.error(f"❌ ONNX load failed: {e}")
            self._use_fallback()
    
    def _use_fallback(self):
        """Use simple sentence-transformers as fallback"""
        try:
            from sentence_transformers import SentenceTransformer
            
            logger.info("🔄 Using sentence-transformers fallback...")
            self.model = SentenceTransformer(self.model_name)
            self.dimension = 384
            
            logger.info(f"✅ Fallback model loaded (384-D)")
            
        except Exception as e:
            logger.error(f"❌ Fallback failed: {e}")
            self.model = None
    
    def encode(self, texts, batch_size=32, show_progress_bar=False):
        """Encode texts to embeddings"""
        if self.model is None:
            logger.warning("⚠️ No model loaded, returning zeros")
            if isinstance(texts, str):
                return [0.0] * self.dimension
            return [[0.0] * self.dimension] * len(texts)
        
        try:
            # ONNX model
            if hasattr(self, 'tokenizer'):
                inputs = self.tokenizer(
                    texts,
                    padding=True,
                    truncation=True,
                    max_length=128,
                    return_tensors="pt"
                )
                
                import torch
                with torch.no_grad():
                    outputs = self.model(**inputs)
                    embeddings = outputs.last_hidden_state[:, 0, :]
                
                # Normalize
                import torch.nn.functional as F
                embeddings = F.normalize(embeddings, p=2, dim=1)
                
                if isinstance(texts, str):
                    return embeddings[0].tolist()
                return embeddings.tolist()
            
            # Sentence transformers
            else:
                embeddings = self.model.encode(
                    texts,
                    batch_size=batch_size,
                    show_progress_bar=show_progress_bar,
                    convert_to_numpy=True,
                    normalize_embeddings=True
                )
                
                if isinstance(texts, str):
                    return embeddings.tolist()
                return embeddings.tolist()
                
        except Exception as e:
            logger.error(f"❌ Encode failed: {e}")
            # Return zeros on error
            if isinstance(texts, str):
                return [0.0] * self.dimension
            return [[0.0] * self.dimension] * len(texts)
    
    def __call__(self, texts, **kwargs):
        """Allow calling like function"""
        return self.encode(texts, **kwargs)


# Quick test
if __name__ == "__main__":
    import time
    
    print("⏱️ Testing fast embeddings...")
    start = time.time()
    
    emb = FastEmbeddings()
    
    elapsed = time.time() - start
    print(f"✅ Loaded in {elapsed:.2f}s")
    
    # Test encoding
    test_text = "Hello world"
    start = time.time()
    result = emb.encode(test_text)
    elapsed = time.time() - start
    
    print(f"✅ Encoded in {elapsed:.3f}s, dim={len(result)}")
