import os
import logging
import numpy as np
from typing import List, Union

try:
    import torch
except ImportError:
    torch = None

try:
    from transformers import AutoTokenizer
except ImportError:
    AutoTokenizer = None

logger = logging.getLogger(__name__)

class OptimizedEmbeddings:
    """
    High-performance embedding wrapper for BAAI/bge-m3 using ONNX Runtime.
    Optimized for vCPU environments with sub-second cold starts and low latency.
    """

    def __init__(self, model_path: str, model_name: str = "BAAI/bge-m3", device: str = "cpu"):
        self.model_name = model_name
        self.device = device

        logger.info(f"🚀 Loading optimized ONNX model from {model_path}...")
        self._use_sentence_transformers = False
        self._use_raw_ort = False
        self.tokenizer = None

        # Try raw ORT session first — works with any ONNX output naming (token_embeddings OR last_hidden_state)
        onnx_loaded = False
        try:
            import onnxruntime as ort
            # Resolve snapshot directory: local path OR HF cache (local_files_only, no download)
            snap_dir = model_path
            if not os.path.isabs(snap_dir):
                from huggingface_hub import snapshot_download
                # Try local cache first; only attempt download if not cached
                try:
                    snap_dir = snapshot_download(model_path, local_files_only=True)
                except Exception:
                    snap_dir = snapshot_download(model_path)
            onnx_file = os.path.join(snap_dir, "onnx", "model.onnx")
            if not os.path.exists(onnx_file):
                onnx_file = os.path.join(snap_dir, "model.onnx")
            if not os.path.exists(onnx_file):
                raise FileNotFoundError(f"No model.onnx found in {snap_dir}")
            self.tokenizer = AutoTokenizer.from_pretrained(
                snap_dir,
                use_fast=True,
                clean_up_tokenization_spaces=True
            )
            self._ort_session = ort.InferenceSession(
                onnx_file,
                providers=["CPUExecutionProvider"]
            )
            out_names = [o.name for o in self._ort_session.get_outputs()]
            # If model already outputs pooled sentence embeddings, use them directly (no mean pooling needed)
            self._ort_sentence_out_idx = None
            self._ort_token_out_idx = 0
            for i, name in enumerate(out_names):
                if name == "sentence_embedding":
                    self._ort_sentence_out_idx = i
                elif "last_hidden_state" in name or "token_embeddings" in name:
                    self._ort_token_out_idx = i
            self._use_raw_ort = True
            onnx_loaded = True
            mode = f"sentence_embedding[{self._ort_sentence_out_idx}]" if self._ort_sentence_out_idx is not None else f"mean_pool[{self._ort_token_out_idx}]"
            logger.info(f"✅ ONNX model loaded (raw ORT) from {onnx_file}, output mode={mode}")
        except Exception as e:
            logger.warning(f"⚠️ ONNX load failed ({e}), trying sentence-transformers fallback...")

        # Fallback to sentence-transformers (local dev / offline)
        if not onnx_loaded:
            try:
                from sentence_transformers import SentenceTransformer
                # Map Xenova ONNX model name → standard sentence-transformers name
                st_model = model_path
                if "Xenova/" in model_path:
                    st_model = model_path.replace("Xenova/", "sentence-transformers/")
                logger.info(f"📦 Loading via sentence-transformers: {st_model}")
                self._st_model = SentenceTransformer(st_model)
                self._use_sentence_transformers = True
                logger.info(f"✅ sentence-transformers fallback loaded: {st_model}")
            except Exception as e2:
                logger.error(f"❌ Both ONNX and sentence-transformers failed: {e2}")
                raise

        # Detect dimension
        test_sentence = "This is a test to detect dimension."
        self.dimension = len(self.embed_query(test_sentence))
        logger.info(f"✅ Embeddings ready ({self.dimension}-D, onnx={onnx_loaded})")

    def _mean_pooling_np(self, token_embeddings: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
        """Mean pooling on numpy arrays (raw ORT path)."""
        mask = attention_mask[:, :, np.newaxis].astype(np.float32)
        return (token_embeddings * mask).sum(axis=1) / np.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)

    def _mean_pooling(self, model_output, attention_mask):
        """Mean pooling on torch tensors (legacy ORTModelForFeatureExtraction path)."""
        if torch is None:
            raise ImportError("torch is required for _mean_pooling")
        token_embeddings = model_output[0]
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        return torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(input_mask_expanded.sum(1), min=1e-9)

    def encode(self, sentences: Union[str, List[str]], convert_to_numpy: bool = True, normalize_embeddings: bool = True) -> Union[np.ndarray, 'torch.Tensor']:
        """Replacement for SentenceTransformer.encode"""
        if isinstance(sentences, str):
            sentences = [sentences]

        # sentence-transformers fallback path
        if self._use_sentence_transformers:
            result = self._st_model.encode(sentences, normalize_embeddings=normalize_embeddings, show_progress_bar=False)
            return result if convert_to_numpy else torch.tensor(result)

        # Raw ORT path (works with any ONNX output naming)
        if self._use_raw_ort:
            encoded = self.tokenizer(sentences, padding=True, truncation=True, max_length=512)
            valid_input_names = {i.name for i in self._ort_session.get_inputs()}
            ort_inputs = {
                "input_ids": np.array(encoded["input_ids"], dtype=np.int64),
                "attention_mask": np.array(encoded["attention_mask"], dtype=np.int64),
            }
            if "token_type_ids" in encoded and "token_type_ids" in valid_input_names:
                ort_inputs["token_type_ids"] = np.array(encoded["token_type_ids"], dtype=np.int64)
            ort_outputs = self._ort_session.run(None, ort_inputs)
            if self._ort_sentence_out_idx is not None:
                # Model already outputs pooled sentence embeddings (SentenceTransformer ONNX export)
                sentence_embeddings = ort_outputs[self._ort_sentence_out_idx]  # [B, hidden]
            else:
                # Need to apply mean pooling over token embeddings
                token_emb = ort_outputs[self._ort_token_out_idx]  # [B, seq, hidden]
                sentence_embeddings = self._mean_pooling_np(token_emb, ort_inputs["attention_mask"])
            if normalize_embeddings:
                norms = np.linalg.norm(sentence_embeddings, axis=1, keepdims=True)
                sentence_embeddings = sentence_embeddings / np.clip(norms, a_min=1e-9, a_max=None)
            if convert_to_numpy:
                return sentence_embeddings
            return torch.tensor(sentence_embeddings)

        # Should not reach here
        raise RuntimeError("No embedding backend loaded")

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Compatible with LangChain style."""
        embeddings = self.encode(texts)
        return embeddings.tolist()

    def embed_query(self, text: str) -> List[float]:
        """Compatible with LangChain style."""
        embedding = self.encode([text])[0]
        return embedding.tolist()
