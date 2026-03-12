# core/processing/ingestion.py

import os
import shutil
import uuid
import logging
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import UploadFile
from qdrant_client import QdrantClient
from qdrant_client.http import models

# Local imports - handle both direct and relative execution
try:
    from core.processing.doc_processor import DocumentProcessor
    from core.processing.semantic_chunker import DocumentChunker
    from models.hivemind_schema import (
        general_kb_payload,
        website_map_payload,
        agent_skill_payload,
        agent_rule_payload,
    )
except ImportError:
    import sys
    sys.path.append(str(Path(__file__).parent.parent.parent))
    from core.processing.doc_processor import DocumentProcessor
    from core.processing.semantic_chunker import DocumentChunker
    from models.hivemind_schema import (
        general_kb_payload,
        website_map_payload,
        agent_skill_payload,
        agent_rule_payload,
    )

logger = logging.getLogger(__name__)

# Constants matching populate_local_qdrant.py
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "tara_case_memory")
EMBEDDING_MODEL = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"

class IngestionService:
    def __init__(self, upload_dir: str = "/tmp/uploads", embeddings: Any = None, qdrant_backend: Any = None):
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
        self.qdrant_backend = qdrant_backend
        
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

    @staticmethod
    def _normalize_upload_type(doc_type: str) -> str:
        raw = (doc_type or "General").strip()
        upper = raw.upper().replace(" ", "_")
        aliases = {
            "GENERAL": "GENERAL_KB",
            "GENERAL_KB": "GENERAL_KB",
            "INTERNAL_KB": "GENERAL_KB",
            "KB": "GENERAL_KB",
            "AGENT_SKILL": "AGENT_SKILL",
            "SKILL": "AGENT_SKILL",
            "AGENT_RULE": "AGENT_RULE",
            "RULE": "AGENT_RULE",
            "WEBSITE_MAP": "WEBSITE_MAP",
            "SITEMAP": "WEBSITE_MAP",
        }
        return aliases.get(upper, "GENERAL_KB")

    async def _resolve_qdrant_target(self, tenant_id: str, dimension: int):
        """
        Resolve the correct Qdrant client and collection for the given tenant.
        Falls back to the default collection when tenant-aware backend is unavailable.
        """
        if self.qdrant_backend and getattr(self.qdrant_backend, "enabled", False):
            sync_client, _, collection_name = self.qdrant_backend._get_clients_for_tenant(tenant_id)  # pylint: disable=protected-access
            if not sync_client or not collection_name:
                raise ValueError(f"Tenant Qdrant target unavailable for tenant={tenant_id}")
            self.qdrant_backend._ensure_collection_for(sync_client, collection_name)  # pylint: disable=protected-access
            return sync_client, collection_name

        await self._ensure_collection_exists(dimension)
        return self.client, self.collection_name

    @staticmethod
    def _build_payload(
        normalized_type: str,
        *,
        text: str,
        tenant_id: str,
        filename: str,
        original_doc_type: str,
        topic: str,
        chunk_index: int,
        doc_id: str,
    ) -> Dict[str, Any]:
        if normalized_type == "AGENT_SKILL":
            return agent_skill_payload(
                text=text,
                topic=topic or "general",
                tenant_id=tenant_id,
            )
        if normalized_type == "AGENT_RULE":
            return agent_rule_payload(
                text=text,
                topic=topic or "general",
                tenant_id=tenant_id,
                severity="standard",
            )
        if normalized_type == "WEBSITE_MAP":
            return website_map_payload(
                url=filename,
                concept=text,
                domain="all",
                tenant_id=tenant_id,
            )
        return general_kb_payload(
            text=text,
            filename=filename,
            doc_type_detail=original_doc_type or "General",
            tenant_id=tenant_id,
            chunk_index=chunk_index,
            doc_id=doc_id,
            topics=topic,
        )

    async def ingest_file(self, file: UploadFile, doc_type: str = "General", topics: str = "", tenant_id: str = "tara") -> Dict[str, Any]:
        """
        Process an uploaded file and ingest into Qdrant using Universal Schema.
        """
        file_id = str(uuid.uuid4())
        filename = file.filename
        file_path = self.upload_dir / f"{file_id}_{filename}"
        normalized_type = self._normalize_upload_type(doc_type)
        
        logger.info(
            f"Receiving file: {filename} (ID: {file_id}) | Type: {doc_type} "
            f"(normalized={normalized_type}) | Tenant: {tenant_id}"
        )
        
        try:
            # 1. Save uploaded file to temp path
            with open(file_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
            
            # 2. Extract Text (for all document types)
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
                
            dimension = embeddings.shape[1]
            target_client, collection_name = await self._resolve_qdrant_target(tenant_id, dimension)
            
            # 6. Prepare Points with Universal Schema
            points = []
            for i, chunk in enumerate(chunks):
                vector = embeddings[i].tolist()
                
                payload = self._build_payload(
                    normalized_type,
                    text=chunk["text"],
                    tenant_id=tenant_id,
                    filename=filename,
                    original_doc_type=doc_type,
                    topic=topics,
                    chunk_index=i,
                    doc_id=doc_data["doc_id"],
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
            
            logger.info(f"Upserting {len(points)} points to Qdrant collection {collection_name}...")

            target_client.upsert(
                collection_name=collection_name,
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
                "collection": collection_name,
                "tenant_id": tenant_id,
                "doc_type": normalized_type,
            }
            
        except Exception as e:
            logger.exception(f"Ingestion failed: {e}")
            raise
            
        finally:
            # Cleanup temp file
            if file_path.exists():
                os.remove(file_path)
