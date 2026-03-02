#!/bin/bash
# Pre-load ONNX model cache for faster RAG startup

echo "🚀 Pre-loading ONNX model cache..."

# Set environment to force cache-only loading
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1

python3 << 'EOF'
import time
import sys

print("⏳ Loading model...")
start = time.time()

try:
    # Try optimum ONNX first (fastest)
    from optimum.onnxruntime import ORTModelForFeatureExtraction
    from transformers import AutoTokenizer
    
    model_name = "Xenova/all-MiniLM-L6-v2"
    print(f"📦 Loading {model_name} from cache...")
    
    tokenizer = AutoTokenizer.from_pretrained(
        model_name,
        local_files_only=True
    )
    
    model = ORTModelForFeatureExtraction.from_pretrained(
        model_name,
        from_transformers=True,
        local_files_only=True
    )
    
    elapsed = time.time() - start
    print(f"✅ ONNX model loaded in {elapsed:.2f}s")
    
except Exception as e:
    print(f"⚠️ ONNX not available: {e}")
    print("📦 Falling back to sentence-transformers...")
    
    try:
        from sentence_transformers import SentenceTransformer
        
        model_name = "all-MiniLM-L6-v2"
        print(f"📦 Loading {model_name}...")
        
        model = SentenceTransformer(model_name)
        
        elapsed = time.time() - start
        print(f"✅ SentenceTransformer loaded in {elapsed:.2f}s")
        
    except Exception as e2:
        print(f"❌ Failed: {e2}")
        sys.exit(1)

# Test encoding
test_text = "Hello world"
start = time.time()
emb = model.encode(test_text) if hasattr(model, 'encode') else None
elapsed = time.time() - start

if emb is not None:
    print(f"✅ Encoding test passed in {elapsed:.3f}s (dim={len(emb)})")
else:
    print("⚠️ Encoding test skipped")

print("✅ Model pre-loaded and cached in memory")
EOF

echo "✅ Model cache ready"
