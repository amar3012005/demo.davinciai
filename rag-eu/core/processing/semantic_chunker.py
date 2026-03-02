# core/processing/semantic_chunker.py

import json
import logging
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import tiktoken

# Import OptimizedEmbeddings from root
try:
    from optimized_embeddings import OptimizedEmbeddings
except ImportError:
    # Fallback for relative import if run differently
    import sys
    sys.path.append(str(Path(__file__).parent.parent.parent))
    from optimized_embeddings import OptimizedEmbeddings

logger = logging.getLogger(__name__)

class DocumentChunker:
    """
    Enhanced semantic chunker using Optimized ONNX embeddings.
    """

    def __init__(self, embedding_model_path: str = "models/bge-m3-onnx", method: str = "semantic_embedding", embeddings: Any = None):
        self.method = method
        self.chunk_size = 1000
        self.chunk_overlap = 200
        self.buffer_size = 2
        self.breakpoint_percentile = 85
        self.embedding_delay = 0.0
        self.batch_size = 32
        
        self.track_performance = False
        
        # Initialize embeddings
        if self.method == "semantic_embedding":
            if embeddings:
                self.embeddings = embeddings
                self.embedding_dim = self.embeddings.dimension
                logger.info(f"✅ Using shared embeddings instance (Dim: {self.embedding_dim})")
            else:
                self._init_embeddings(embedding_model_path)
        else:
            self.embeddings = None
        
        self._init_tokenizer()

    def _init_embeddings(self, model_path: str):
        """Initialize Optimized ONNX embeddings."""
        print(f"Initializing embeddings from {model_path}...")
        try:
            # Allow HuggingFace Hub IDs by removing local path check
            # if not Path(model_path).exists(): ...
                 
            self.embeddings = OptimizedEmbeddings(model_path=model_path)
            self.embedding_dim = self.embeddings.dimension
            print(f"✅ Embeddings initialized (Dim: {self.embedding_dim})")
        except Exception as e:
            print(f"Error initializing embeddings: {e}")
            self.embeddings = None
            self.method = "simple" # Fallback

    def _init_tokenizer(self):
        try:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
            self.use_tiktoken = True
        except Exception:
            self.tokenizer = None
            self.use_tiktoken = False

    def count_tokens(self, text: str) -> int:
        if self.use_tiktoken:
            return len(self.tokenizer.encode(text))
        else:
            return len(text) // 4

    def _detect_german(self, text: str) -> bool:
        german_chars = ["ä", "ö", "ü", "ß"]
        text_lower = text.lower()
        return any(char in text_lower for char in german_chars)

    def _compute_embeddings_batch(self, texts: List[str]) -> np.ndarray:
        if not self.embeddings:
            return np.array([])
        
        try:
            # OptimizedEmbeddings.embed_documents returns List[List[float]]
            embeddings = self.embeddings.embed_documents(texts)
            return np.array(embeddings)
        except Exception as e:
            print(f"Error computing embeddings: {e}")
            return np.array([])

    def _split_into_sentences(self, text: str) -> List[str]:
        if self._detect_german(text):
            sentences = re.split(r'(?<=[.!?])\s+(?=[A-ZÄÖÜ"\'])', text)
        else:
            sentences = re.split(r'(?<=[.?!])\s+(?=[A-Z"\'])', text)
            
        # Basic cleanup
        return [s.strip() for s in sentences if s.strip()]

    def _create_sliding_windows(self, sentences: List[str]) -> List[str]:
        if len(sentences) <= 1:
            return sentences
        
        windows = []
        for i in range(len(sentences)):
            start = max(0, i - self.buffer_size)
            end = min(len(sentences), i + self.buffer_size + 1)
            window_text = " ".join(sentences[start:end])
            windows.append(window_text)
        return windows

    def _calculate_distances_fast(self, embeddings: np.ndarray) -> List[float]:
        try:
            # Normalize
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / (norms + 1e-10)
            
            # Cosine similarity
            similarities = np.sum(embeddings[:-1] * embeddings[1:], axis=1)
            return (1 - similarities).tolist()
        except Exception:
            return []

    def _find_breakpoints(self, distances: List[float]) -> List[int]:
        if not distances:
            return []
        threshold = np.percentile(distances, self.breakpoint_percentile)
        return [i for i, d in enumerate(distances) if d > threshold]

    def _create_chunks_from_sentences(self, sentences: List[str], breakpoints: List[int]) -> List[str]:
        chunks = []
        start_idx = 0
        for breakpoint in breakpoints:
            end_idx = breakpoint + 1
            chunks.append(" ".join(sentences[start_idx:end_idx]))
            start_idx = end_idx
        if start_idx < len(sentences):
            chunks.append(" ".join(sentences[start_idx:]))
        return chunks

    def chunk_document(self, doc_id: str, text: str, metadata: Dict = None) -> List[Dict]:
        """Main chunking method"""
        if not text:
            return []
            
        chunks = []
        
        if self.method == "semantic_embedding" and self.embeddings:
            sentences = self._split_into_sentences(text)
            logger.info(f"Splitting text into {len(sentences)} sentences for analysis")
            
            if len(sentences) > 3:
                windows = self._create_sliding_windows(sentences)
                embeddings = self._compute_embeddings_batch(windows)
                
                if embeddings.size > 0:
                    distances = self._calculate_distances_fast(embeddings)
                    breakpoints = self._find_breakpoints(distances)
                    raw_chunks = self._create_chunks_from_sentences(sentences, breakpoints)
                    logger.info(f"Created {len(raw_chunks)} semantic chunks from {len(sentences)} sentences")
                else:
                    raw_chunks = [text] # Fallback
            else:
                raw_chunks = [text]
        else:
            # Simple fallback
            raw_chunks = [text] # TO DO: Implement simple chunking if needed

        # Format chunks
        for i, chunk_text in enumerate(raw_chunks):
             chunks.append({
                 "chunk_id": f"{doc_id}_chunk_{i+1:03d}",
                 "text": chunk_text,
                 "doc_id": doc_id,
                 "metadata": metadata or {}
             })
             
        return chunks
