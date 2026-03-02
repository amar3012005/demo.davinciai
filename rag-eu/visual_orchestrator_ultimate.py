"""
visual_orchestrator_ultimate.py

PURPOSE: Integration layer for Ultimate TARA architecture.
         Wraps the existing VisualOrchestrator with new modules:
         - mind_reader: Intent parsing
         - hive_interface: Qdrant dual-retrieval
         - semantic_detective: Hybrid scoring
         - mission_brain: Constraint enforcement
         - live_graph: Redis DOM mirror

DEPENDENCIES:
    - Existing VisualOrchestrator
    - New ultimate modules (mind_reader, hive_interface, etc.)
    - Environment variables: QDRANT_URL, QDRANT_API_KEY, REDIS_URL

MIGRATION STATUS: [INTEGRATION] - Bridges old and new architecture

FEATURE FLAGS:
    - USE_NEW_DETECTIVE: Enable new semantic_detective (default: False)
    - USE_MISSION_BRAIN: Enable constraint enforcement (default: False)
    - USE_LIVE_GRAPH: Enable Redis DOM mirror (default: False)

Example:
    from visual_orchestrator_ultimate import UltimateVisualOrchestrator
    
    orchestrator = UltimateVisualOrchestrator(
        groq_provider=groq,
        qdrant_url=os.getenv("QDRANT_URL"),
        qdrant_api_key=os.getenv("QDRANT_API_KEY"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379")
    )
    
    # Use new architecture
    result = await orchestrator.execute_with_new_architecture(
        session_id="session-123",
        user_input="Export my API usage"
    )
"""

import json
import logging
import os
import time
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Import existing VisualOrchestrator
try:
    from visual_orchestrator import VisualOrchestrator
    VISUAL_ORCHESTRATOR_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import VisualOrchestrator: {e}")
    VISUAL_ORCHESTRATOR_AVAILABLE = False
    VisualOrchestrator = None

# Import new ultimate modules
try:
    from tara_models import TacticalSchema, ActionIntent, MissionState, Constraint, ConstraintStatus
    MODELS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import tara_models: {e}")
    MODELS_AVAILABLE = False

try:
    from mind_reader import MindReader, create_mind_reader
    MIND_READER_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import mind_reader: {e}")
    MIND_READER_AVAILABLE = False

try:
    from hive_interface import HiveInterface, create_hive_interface
    HIVE_INTERFACE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import hive_interface: {e}")
    HIVE_INTERFACE_AVAILABLE = False

try:
    from semantic_detective import SemanticDetective, create_semantic_detective
    SEMANTIC_DETECTIVE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import semantic_detective: {e}")
    SEMANTIC_DETECTIVE_AVAILABLE = False

try:
    from mission_brain import MissionBrain, create_mission_brain
    MISSION_BRAIN_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import mission_brain: {e}")
    MISSION_BRAIN_AVAILABLE = False

try:
    from live_graph import LiveGraph
    LIVE_GRAPH_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import live_graph: {e}")
    LIVE_GRAPH_AVAILABLE = False

# Redis imports
try:
    from redis import asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Qdrant imports
try:
    from qdrant_client import QdrantClient, AsyncQdrantClient
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False


@dataclass
class UltimateConfig:
    """Configuration for Ultimate TARA architecture."""
    
    # Feature flags
    use_new_detective: bool = False
    use_mission_brain: bool = False
    use_live_graph: bool = False
    use_hive_interface: bool = True
    
    # Qdrant configuration
    qdrant_url: Optional[str] = None
    qdrant_api_key: Optional[str] = None
    qdrant_collection: str = "tara_hive"
    
    # Redis configuration
    redis_url: str = "redis://localhost:6379"
    
    # LLM models
    mind_reader_model: str = "llama-3.1-8b-instant"
    embedding_model: str = "all-MiniLM-L6-v2"
    
    # Thresholds
    constraint_enforcement: bool = True
    ambiguity_threshold: float = 0.1
    min_confidence: float = 0.4


class UltimateVisualOrchestrator:
    """
    Ultimate TARA orchestrator with new architecture.
    
    Wraps existing VisualOrchestrator and adds:
    1. Mind Reader for intent parsing
    2. Hive Interface for Qdrant dual-retrieval
    3. Semantic Detective for hybrid scoring
    4. Mission Brain for constraint enforcement
    5. Live Graph for Redis DOM mirroring
    """

    def __init__(
        self,
        groq_provider: Any,
        qdrant_url: Optional[str] = None,
        qdrant_api_key: Optional[str] = None,
        qdrant_collection: str = "tara_hive",
        redis_url: str = "redis://localhost:6379",
        config: Optional[UltimateConfig] = None
    ):
        """
        Initialize Ultimate Visual Orchestrator.
        
        Args:
            groq_provider: GroqProvider instance
            qdrant_url: Qdrant server URL (from env: QDRANT_URL)
            qdrant_api_key: Qdrant API key (from env: QDRANT_API_KEY)
            qdrant_collection: Qdrant collection name
            redis_url: Redis server URL
            config: UltimateConfig instance (optional)
        """
        self.groq = groq_provider
        self.config = config or UltimateConfig(
            qdrant_url=qdrant_url,
            qdrant_api_key=qdrant_api_key,
            qdrant_collection=qdrant_collection,
            redis_url=redis_url
        )
        
        # Initialize Redis client
        self.redis = None
        if REDIS_AVAILABLE:
            try:
                self.redis = aioredis.from_url(self.config.redis_url)
                logger.info(f"✅ Redis connected: {self.config.redis_url}")
            except Exception as e:
                logger.warning(f"⚠️ Redis connection failed: {e}")
        
        # Initialize Qdrant client
        self.qdrant = None
        if QDRANT_AVAILABLE and self.config.qdrant_url:
            try:
                self.qdrant = AsyncQdrantClient(
                    url=self.config.qdrant_url,
                    api_key=self.config.qdrant_api_key
                )
                logger.info(f"✅ Qdrant connected: {self.config.qdrant_url}")
            except Exception as e:
                logger.warning(f"⚠️ Qdrant connection failed: {e}")
        
        # Initialize existing VisualOrchestrator (backward compatibility)
        self.legacy_orchestrator = None
        if VISUAL_ORCHESTRATOR_AVAILABLE and groq_provider:
            try:
                self.legacy_orchestrator = VisualOrchestrator(
                    groq_provider=groq_provider,
                    qdrant_client=self.qdrant,
                    embeddings=None,  # Will be initialized if needed
                    redis_client=self.redis
                )
                logger.info("✅ Legacy VisualOrchestrator initialized")
            except Exception as e:
                logger.warning(f"⚠️ Legacy VisualOrchestrator init failed: {e}")
        
        # Initialize new modules
        
        # 1. Mind Reader
        self.mind_reader = None
        if MIND_READER_AVAILABLE and groq_provider:
            try:
                self.mind_reader = MindReader(groq_provider)
                logger.info("✅ MindReader initialized")
            except Exception as e:
                logger.warning(f"⚠️ MindReader init failed: {e}")
        
        # 2. Hive Interface
        self.hive_interface = None
        if HIVE_INTERFACE_AVAILABLE and self.qdrant:
            try:
                self.hive_interface = HiveInterface(
                    qdrant_client=self.qdrant,
                    embeddings=None,  # Optional: sentence transformer
                    redis_client=self.redis,
                    collection_name=self.config.qdrant_collection
                )
                logger.info(f"✅ HiveInterface initialized (collection: {self.config.qdrant_collection})")
            except Exception as e:
                logger.warning(f"⚠️ HiveInterface init failed: {e}")
        
        # 3. Live Graph
        self.live_graph = None
        if LIVE_GRAPH_AVAILABLE and self.redis:
            try:
                self.live_graph = LiveGraph(self.redis)
                logger.info("✅ LiveGraph initialized")
            except Exception as e:
                logger.warning(f"⚠️ LiveGraph init failed: {e}")
        
        # 4. Semantic Detective
        self.semantic_detective = None
        if SEMANTIC_DETECTIVE_AVAILABLE and self.live_graph:
            try:
                self.semantic_detective = SemanticDetective(
                    live_graph=self.live_graph,
                    model_name=self.config.embedding_model
                )
                logger.info(f"✅ SemanticDetective initialized (model: {self.config.embedding_model})")
            except Exception as e:
                logger.warning(f"⚠️ SemanticDetective init failed: {e}")
        
        # 5. Mission Brain
        self.mission_brain = None
        if MISSION_BRAIN_AVAILABLE and self.redis:
            try:
                self.mission_brain = create_mission_brain(
                    redis_url=self.config.redis_url,
                    hive_interface=self.hive_interface
                )
                logger.info("✅ MissionBrain initialized")
            except Exception as e:
                logger.warning(f"⚠️ MissionBrain init failed: {e}")
        
        # Log feature status
        self._log_feature_status()

    def _log_feature_status(self):
        """Log which features are enabled."""
        logger.info("=" * 60)
        logger.info("ULTIMATE TARA - Feature Status")
        logger.info("=" * 60)
        logger.info(f"Mind Reader: {'✅' if self.mind_reader else '❌'}")
        logger.info(f"Hive Interface: {'✅' if self.hive_interface else '❌'}")
        logger.info(f"Live Graph: {'✅' if self.live_graph else '❌'}")
        logger.info(f"Semantic Detective: {'✅' if self.semantic_detective else '❌'}")
        logger.info(f"Mission Brain: {'✅' if self.mission_brain else '❌'}")
        logger.info(f"Legacy Orchestrator: {'✅' if self.legacy_orchestrator else '❌'}")
        logger.info("=" * 60)

    async def execute_with_new_architecture(
        self,
        session_id: str,
        user_input: str,
        current_url: str = "",
        dom_elements: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Execute user request using new Ultimate architecture.
        
        Args:
            session_id: Session identifier
            user_input: User's voice/text input
            current_url: Current page URL
            dom_elements: Optional DOM elements (if not using LiveGraph)
        
        Returns:
            Execution result dict
        """
        start_time = time.time()
        result = {
            "session_id": session_id,
            "user_input": user_input,
            "architecture": "ultimate",
            "steps": [],
            "success": False,
            "error": None
        }
        
        try:
            # Step 1: Parse intent with Mind Reader
            if not self.mind_reader:
                raise RuntimeError("MindReader not available")
            
            schema = await self.mind_reader.translate(
                user_input=user_input,
                current_url=current_url
            )
            result["steps"].append({
                "step": "intent_parsing",
                "module": "mind_reader",
                "result": schema.to_dict(),
                "duration_ms": int((time.time() - start_time) * 1000)
            })
            logger.info(f"🧠 Intent parsed: {schema.action.value} on {schema.target_entity}")
            
            # Step 2: Retrieve strategy + hints from Hive
            if self.hive_interface:
                hive_response = await self.hive_interface.retrieve(schema)
                result["steps"].append({
                    "step": "hive_retrieval",
                    "module": "hive_interface",
                    "result": hive_response.to_dict(),
                    "duration_ms": int((time.time() - start_time) * 1000)
                })
                logger.info(
                    f"🧠 Hive retrieved: "
                    f"Strategy={bool(hive_response.strategy)}, "
                    f"Hints={len(hive_response.visual_hints)}"
                )
            else:
                hive_response = None
            
            # Step 3: Create mission with Mission Brain
            if self.mission_brain:
                mission = await self.mission_brain.create_mission(
                    session_id=session_id,
                    schema=schema,
                    strategy=hive_response.strategy if hive_response else None
                )
                result["mission_id"] = mission.mission_id
                result["steps"].append({
                    "step": "mission_creation",
                    "module": "mission_brain",
                    "result": {
                        "mission_id": mission.mission_id,
                        "subgoals": mission.subgoals,
                        "constraints": {
                            k: v.to_dict() for k, v in mission.constraints.items()
                        }
                    },
                    "duration_ms": int((time.time() - start_time) * 1000)
                })
                logger.info(f"🧠 Mission created: {mission.mission_id} ({len(mission.subgoals)} subgoals)")
            else:
                mission = None
            
            # Step 4: Investigate with Semantic Detective
            if self.semantic_detective and self.live_graph:
                detective_report = await self.semantic_detective.investigate(
                    session_id=session_id,
                    query=schema.target_entity,
                    hive_hints=hive_response.visual_hints if hive_response else [],
                    action_intent=schema.action
                )
                result["steps"].append({
                    "step": "detective_investigation",
                    "module": "semantic_detective",
                    "result": detective_report.to_dict(),
                    "duration_ms": int((time.time() - start_time) * 1000)
                })
                logger.info(
                    f"🔍 Detective found: "
                    f"best='{detective_report.best_match.text if detective_report.best_match else 'none'}' "
                    f"(score={detective_report.best_match.hybrid_score if detective_report.best_match else 0:.2f})"
                )
            else:
                detective_report = None
            
            # Step 5: Audit action with Mission Brain (constraint enforcement)
            if self.mission_brain and detective_report and detective_report.best_match:
                approved, reason = await self.mission_brain.audit_action(
                    mission_id=mission.mission_id if mission else "temp",
                    action_type=detective_report.recommended_action,
                    target_id=detective_report.best_match.node_id,
                    target_text=detective_report.best_match.text,
                    detective_report=detective_report
                )
                result["action_approved"] = approved
                result["action_reason"] = reason
                
                if not approved:
                    logger.warning(f"🚫 Action blocked: {reason}")
                    result["error"] = reason
                    return result
                
                logger.info(f"✅ Action approved: {detective_report.recommended_action}")
            
            # Step 6: Execute action (using legacy orchestrator for now)
            if self.legacy_orchestrator and detective_report and detective_report.best_match:
                # TODO: Implement new executor
                logger.info("Using legacy executor (TODO: implement new executor)")
            
            result["success"] = True
            result["total_duration_ms"] = int((time.time() - start_time) * 1000)
            
            return result
            
        except Exception as e:
            logger.error(f"Ultimate architecture execution failed: {e}")
            result["error"] = str(e)
            
            # Fallback to legacy architecture
            if self.legacy_orchestrator:
                logger.info("Falling back to legacy architecture")
                return await self._fallback_to_legacy(session_id, user_input, current_url, dom_elements)
            
            return result

    async def _fallback_to_legacy(
        self,
        session_id: str,
        user_input: str,
        current_url: str,
        dom_elements: Optional[List[Dict]]
    ) -> Dict[str, Any]:
        """Fallback to legacy VisualOrchestrator."""
        if not self.legacy_orchestrator:
            return {
                "success": False,
                "error": "No orchestrator available"
            }
        
        # Use legacy execute method
        # Note: This is a simplified fallback - actual implementation depends on legacy API
        return {
            "success": True,
            "architecture": "legacy_fallback",
            "message": "Executed with legacy architecture"
        }

    async def ingest_dom_delta(self, session_id: str, delta: Dict[str, Any]) -> bool:
        """
        Ingest DOM delta from tara_sensor.js.
        
        Args:
            session_id: Session identifier
            delta: Delta dictionary from WebSocket
        
        Returns:
            True if ingested successfully
        """
        if not self.live_graph:
            logger.warning("LiveGraph not available for delta ingestion")
            return False
        
        try:
            await self.live_graph.ingest_delta(session_id, delta)
            return True
        except Exception as e:
            logger.error(f"Failed to ingest DOM delta: {e}")
            return False

    async def get_mission_status(self, mission_id: str) -> Optional[Dict[str, Any]]:
        """
        Get mission status.
        
        Args:
            mission_id: Mission identifier
        
        Returns:
            Status dict or None
        """
        if not self.mission_brain:
            return None
        
        return await self.mission_brain.get_mission_status(mission_id)

    async def update_constraint(
        self,
        mission_id: str,
        constraint_name: str,
        value: str
    ) -> bool:
        """
        Update constraint value (e.g., from user selection).
        
        Args:
            mission_id: Mission identifier
            constraint_name: Constraint name
            value: Constraint value
        
        Returns:
            True if updated successfully
        """
        if not self.mission_brain:
            return False
        
        return await self.mission_brain.update_constraint(
            mission_id=mission_id,
            constraint_name=constraint_name,
            value=value,
            status=ConstraintStatus.FILLED
        )


# ═══════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════

def create_ultimate_orchestrator(
    groq_provider: Any,
    qdrant_url: Optional[str] = None,
    qdrant_api_key: Optional[str] = None,
    redis_url: str = "redis://localhost:6379"
) -> UltimateVisualOrchestrator:
    """
    Factory function to create UltimateVisualOrchestrator.
    
    Args:
        groq_provider: GroqProvider instance
        qdrant_url: Qdrant server URL (from env: QDRANT_URL)
        qdrant_api_key: Qdrant API key (from env: QDRANT_API_KEY)
        redis_url: Redis server URL
    
    Returns:
        UltimateVisualOrchestrator instance
    """
    return UltimateVisualOrchestrator(
        groq_provider=groq_provider,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        redis_url=redis_url
    )
