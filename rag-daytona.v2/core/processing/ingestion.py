# core/processing/ingestion.py

import os
import shutil
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import UploadFile
from qdrant_client import QdrantClient
from qdrant_client.http import models

# Local imports - handle both direct and relative execution
try:
    from core.processing.doc_processor import DocumentProcessor
    from core.processing.semantic_chunker import DocumentChunker
except ImportError:
    import sys
    sys.path.append(str(Path(__file__).parent.parent.parent))
    from core.processing.doc_processor import DocumentProcessor
    from core.processing.semantic_chunker import DocumentChunker

logger = logging.getLogger(__name__)

# Constants matching populate_local_qdrant.py
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "tara_case_memory")
EMBEDDING_MODEL = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"

class IngestionService:
    def __init__(self, upload_dir: str = "/tmp/uploads", embeddings: Any = None):
        self.upload_dir = Path(upload_dir)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize components
        # Note: We put processed files in a temp subdir
        self.processor = DocumentProcessor(output_dir=str(self.upload_dir / "processed"))
        
        # Initialize chunker with the SAME model as existing system to ensure vector compatibility
        self.chunker = DocumentChunker(
            embedding_model_path=EMBEDDING_MODEL, 
            method="semantic_embedding",
            embeddings=embeddings # Pass shared embeddings
        )
        
        self.client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
        self.collection_name = COLLECTION_NAME
        
        logger.info(f"IngestionService initialized. Target collection: {COLLECTION_NAME}")

    async def _ensure_collection_exists(self, dimension: int):
        """Ensure the collection exists with correct dimensions."""
        try:
            self.client.get_collection(self.collection_name)
        except Exception:
            logger.info(f"Collection {self.collection_name} not found, creating...")
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE)
            )

    async def ingest_file(self, file: UploadFile, doc_type: str = "General", topics: str = "", tenant_id: str = "tara") -> Dict[str, Any]:
        """
        Process an uploaded file and ingest into Qdrant using Universal Schema.
        """
        # Import schema factories
        from .models.hivemind_schema import general_kb_payload, website_map_payload, agent_skill_payload
        
        file_id = str(uuid.uuid4())
        filename = file.filename
        file_path = self.upload_dir / f"{file_id}_{filename}"
        
        logger.info(f"Receiving file: {filename} (ID: {file_id}) | Type: {doc_type} | Tenant: {tenant_id}")
        
        try:
            # 1. Save uploaded file to temp path
            with open(file_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
            
            # 2. Handle different doc_types
            # Normalize doc_type
            type_upper = doc_type.upper()
            
            # Special case: Website Map (JSON/CSV)
            if type_upper == "WEBSITE_MAP" and filename.endswith(('.json', '.csv')):
                # TODO: Implement structured parsing for maps
                # For now, we'll treat it as a document but use the sitemap label
                pass

            # 3. Extract Text (for all document types)
            logger.info("Extracting text...")
            doc_data = self.processor.process_single_file(str(file_path))
            
            if not doc_data:
                logger.error("Text extraction failed or returned empty")
                raise ValueError("Failed to extract legible text from document")
            
            logger.info(f"Extracted {len(doc_data['text'])} characters. Chunking...")
            
            # 4. Chunk Document
            chunks = self.chunker.chunk_document(doc_data["doc_id"], doc_data["text"], doc_data["metadata"])
            
            if not chunks:
                logger.warning("No chunks created")
                return {"status": "warning", "message": "No chunks created from document", "chunks_count": 0}

            # 5. Compute Embeddings
            texts = [c["text"] for c in chunks]
            embeddings = self.chunker._compute_embeddings_batch(texts)
            
            if len(embeddings) == 0:
                raise ValueError("Embedding generation failed")
                
            # Ensure collection exists
            dimension = embeddings.shape[1]
            await self._ensure_collection_exists(dimension)
            
            # 6. Prepare Points with Universal Schema
            points = []
            for i, chunk in enumerate(chunks):
                vector = embeddings[i].tolist()
                
                # Determine factory based on doc_type
                if type_upper == "AGENT_SKILL":
                    payload = agent_skill_payload(
                        text=chunk["text"],
                        topic=topics or "general",
                        tenant_id=tenant_id
                    )
                else:
                    # Default to General KB
                    payload = general_kb_payload(
                        text=chunk["text"],
                        filename=filename,
                        doc_type_detail=doc_type,
                        tenant_id=tenant_id,
                        chunk_index=i,
                        doc_id=doc_data["doc_id"],
                        topics=topics,
                    )
                
                # Merge extra chunk metadata
                for k, v in chunk.get("metadata", {}).items():
                    if k not in payload:
                        payload[k] = v
                
                # Generate deterministic UUID for idempotency
                point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc_data['doc_id']}_{i}"))
                payload.pop("uuid", None)  # remove auto-generated uuid, use deterministic
                
                points.append(models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload
                ))
            
            logger.info(f"Upserting {len(points)} points to Qdrant...")

            
            # 6. Upsert
            self.client.upsert(
                collection_name=self.collection_name,
                points=points
            )
            
            logger.info("Ingestion complete")
            for i, point in enumerate(points):
                logger.info(f"✅ Ingested Chunk {i+1}/{len(points)} | ID: {point.id} | Size: {len(point.payload['text'])} chars")
                logger.debug(f"   Preview: {point.payload['text'][:100]}...")

            return {
                "status": "success",
                "doc_id": doc_data["doc_id"],
                "filename": filename,
                "chunks_ingested": len(points),
                "collection": self.collection_name
            }
            
        except Exception as e:
            logger.exception(f"Ingestion failed: {e}")
            raise
            
        finally:
            # Cleanup temp file
            if file_path.exists():
                os.remove(file_path)
