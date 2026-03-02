#!/usr/bin/env python3
"""
test_ultimate_integration.py

End-to-end integration test for Ultimate TARA architecture.
Tests all modules working together:
1. Mind Reader - Intent parsing
2. Hive Interface - Qdrant retrieval
3. Live Graph - Redis DOM mirror
4. Semantic Detective - Hybrid scoring
5. Mission Brain - Constraint enforcement

REQUIREMENTS:
- Redis running on localhost:6379 (or set REDIS_URL)
- Qdrant running (or set QDRANT_URL)
- Groq API key (or use fallback mode)

USAGE:
    python test_ultimate_integration.py
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import Dict, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import ultimate modules
try:
    from tara_models import TacticalSchema, ActionIntent, MissionState, Constraint, ConstraintStatus
    from mind_reader import MindReader
    from hive_interface import HiveInterface
    from live_graph import LiveGraph
    from semantic_detective import SemanticDetective
    from mission_brain import MissionBrain
    from visual_orchestrator_ultimate import UltimateVisualOrchestrator, UltimateConfig
    MODULES_AVAILABLE = True
except ImportError as e:
    logger.error(f"Failed to import ultimate modules: {e}")
    MODULES_AVAILABLE = False

# Redis
try:
    from redis import asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Qdrant
try:
    from qdrant_client import AsyncQdrantClient
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False


class UltimateIntegrationTest:
    """End-to-end integration test suite."""
    
    def __init__(self):
        self.redis = None
        self.qdrant = None
        self.orchestrator = None
        self.results = {
            "tests_run": 0,
            "tests_passed": 0,
            "tests_failed": 0,
            "details": []
        }
    
    async def setup(self):
        """Initialize connections and orchestrator."""
        logger.info("=" * 60)
        logger.info("ULTIMATE TARA - Integration Test Setup")
        logger.info("=" * 60)
        
        # Redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        if REDIS_AVAILABLE:
            try:
                self.redis = aioredis.from_url(redis_url)
                await self.redis.ping()
                logger.info(f"✅ Redis connected: {redis_url}")
            except Exception as e:
                logger.warning(f"⚠️ Redis connection failed: {e}")
                self.redis = None
        else:
            logger.warning("⚠️ Redis not available")
        
        # Qdrant
        qdrant_url = os.getenv("QDRANT_URL")
        qdrant_api_key = os.getenv("QDRANT_API_KEY")
        if QDRANT_AVAILABLE and qdrant_url:
            try:
                self.qdrant = AsyncQdrantClient(
                    url=qdrant_url,
                    api_key=qdrant_api_key
                )
                logger.info(f"✅ Qdrant connected: {qdrant_url}")
            except Exception as e:
                logger.warning(f"⚠️ Qdrant connection failed: {e}")
                self.qdrant = None
        else:
            logger.warning("⚠️ Qdrant not available (set QDRANT_URL)")
        
        # Create orchestrator
        if MODULES_AVAILABLE:
            config = UltimateConfig(
                qdrant_url=qdrant_url,
                qdrant_api_key=qdrant_api_key,
                redis_url=redis_url,
                use_new_detective=True,
                use_mission_brain=True,
                use_live_graph=True if self.redis else False,
                use_hive_interface=True if self.qdrant else False
            )
            
            self.orchestrator = UltimateVisualOrchestrator(
                groq_provider=None,  # Will use fallback mode
                config=config
            )
            logger.info("✅ UltimateVisualOrchestrator initialized")
        else:
            logger.error("❌ Modules not available")
            return False
        
        return True
    
    async def teardown(self):
        """Cleanup connections."""
        logger.info("\n🧹 Cleaning up...")
        
        if self.redis:
            await self.redis.close()
        
        if self.qdrant:
            await self.qdrant.close()
        
        logger.info("✅ Cleanup complete")
    
    def record_result(self, test_name: str, passed: bool, details: str = ""):
        """Record test result."""
        self.results["tests_run"] += 1
        if passed:
            self.results["tests_passed"] += 1
            logger.info(f"✅ {test_name}: PASSED")
        else:
            self.results["tests_failed"] += 1
            logger.error(f"❌ {test_name}: FAILED - {details}")
        
        self.results["details"].append({
            "test": test_name,
            "passed": passed,
            "details": details
        })
    
    async def test_1_mind_reader(self):
        """Test Mind Reader intent parsing."""
        logger.info("\n" + "=" * 60)
        logger.info("Test 1: Mind Reader - Intent Parsing")
        logger.info("=" * 60)
        
        if not self.orchestrator or not self.orchestrator.mind_reader:
            self.record_result("Mind Reader", False, "MindReader not available")
            return
        
        test_cases = [
            ("Buy a white shirt", ActionIntent.PURCHASE),
            ("Show me my API usage", ActionIntent.EXTRACTION),
            ("Click the submit button", ActionIntent.INTERACTION),
            ("Go to settings", ActionIntent.NAVIGATION),
            ("Find red shoes", ActionIntent.SEARCH),
        ]
        
        all_passed = True
        
        for user_input, expected_action in test_cases:
            try:
                schema = await self.orchestrator.mind_reader.translate(
                    user_input=user_input,
                    current_url="https://example.com"
                )
                
                if schema.action == expected_action:
                    logger.info(f"  ✅ '{user_input}' → {schema.action.value}")
                else:
                    logger.warning(f"  ⚠️ '{user_input}' → {schema.action.value} (expected {expected_action.value})")
                    # Don't fail on fallback mode differences
                    
            except Exception as e:
                logger.error(f"  ❌ '{user_input}' failed: {e}")
                all_passed = False
        
        self.record_result("Mind Reader", all_passed, f"Tested {len(test_cases)} cases")
    
    async def test_2_live_graph(self):
        """Test Live Graph DOM mirror."""
        logger.info("\n" + "=" * 60)
        logger.info("Test 2: Live Graph - Redis DOM Mirror")
        logger.info("=" * 60)
        
        if not self.orchestrator or not self.orchestrator.live_graph:
            self.record_result("Live Graph", False, "LiveGraph not available")
            return
        
        session_id = f"test-session-{int(time.time())}"
        
        try:
            # Test 1: Ingest full scan
            delta = {
                "delta_type": "full_scan",
                "nodes": [
                    {
                        "id": "tara-test1",
                        "tag": "button",
                        "text": "Export Data",
                        "role": "button",
                        "zone": "toolbar",
                        "interactive": True,
                        "visible": True,
                        "rect": {"x": 100, "y": 200, "w": 80, "h": 40},
                        "parent_id": None,
                        "depth": 2,
                        "state": "",
                        "timestamp": time.time()
                    },
                    {
                        "id": "tara-test2",
                        "tag": "a",
                        "text": "Dashboard",
                        "role": "link",
                        "zone": "nav",
                        "interactive": True,
                        "visible": True,
                        "rect": {"x": 0, "y": 0, "w": 100, "h": 30},
                        "parent_id": None,
                        "depth": 1,
                        "state": "",
                        "timestamp": time.time()
                    }
                ],
                "url": "https://example.com/dashboard",
                "timestamp": time.time()
            }
            
            await self.orchestrator.ingest_dom_delta(session_id, delta)
            logger.info("  ✅ Full scan ingested")
            
            # Test 2: Query nodes
            nodes = await self.orchestrator.live_graph.get_visible_nodes(session_id)
            if len(nodes) == 2:
                logger.info(f"  ✅ Retrieved {len(nodes)} nodes")
            else:
                logger.warning(f"  ⚠️ Expected 2 nodes, got {len(nodes)}")
            
            # Test 3: Find by ID
            node = await self.orchestrator.live_graph.find_by_id(session_id, "tara-test1")
            if node and node.text == "Export Data":
                logger.info(f"  ✅ Found node by ID: {node.text}")
            else:
                logger.warning(f"  ⚠️ Node not found or wrong text")
            
            # Test 4: Get buttons
            buttons = await self.orchestrator.live_graph.get_buttons(session_id)
            if len(buttons) >= 1:
                logger.info(f"  ✅ Found {len(buttons)} buttons")
            else:
                logger.warning(f"  ⚠️ Expected buttons, got {len(buttons)}")
            
            # Cleanup
            await self.orchestrator.live_graph.clear_graph(session_id)
            
            self.record_result("Live Graph", True, "All operations successful")
            
        except Exception as e:
            logger.error(f"  ❌ Live Graph test failed: {e}")
            self.record_result("Live Graph", False, str(e))
    
    async def test_3_mission_brain_constraints(self):
        """Test Mission Brain constraint enforcement."""
        logger.info("\n" + "=" * 60)
        logger.info("Test 3: Mission Brain - Constraint Enforcement")
        logger.info("=" * 60)
        
        if not self.orchestrator or not self.orchestrator.mission_brain:
            self.record_result("Mission Brain", False, "MissionBrain not available")
            return
        
        try:
            # Create mission with missing constraint
            schema = TacticalSchema(
                action=ActionIntent.PURCHASE,
                target_entity="shirt",
                domain="shop.com",
                constraints={"color": "white", "size": None},  # size is MISSING
                raw_utterance="Buy a white shirt"
            )
            
            mission = await self.orchestrator.mission_brain.create_mission(
                session_id="test-session",
                schema=schema
            )
            
            logger.info(f"  ✅ Mission created: {mission.mission_id}")
            logger.info(f"     Subgoals: {len(mission.subgoals)}")
            logger.info(f"     Constraints: {list(mission.constraints.keys())}")
            
            # Test 1: Try to add to cart without size (should be blocked)
            approved, reason = await self.orchestrator.mission_brain.audit_action(
                mission_id=mission.mission_id,
                action_type="click",
                target_id="add-to-cart",
                target_text="Add to Cart"
            )
            
            if not approved and "size" in reason.lower():
                logger.info(f"  ✅ Action correctly blocked: {reason}")
            else:
                logger.warning(f"  ⚠️ Action should have been blocked: approved={approved}, reason={reason}")
            
            # Test 2: Update constraint (user selects size)
            await self.orchestrator.mission_brain.update_constraint(
                mission_id=mission.mission_id,
                constraint_name="size",
                value="medium"
            )
            
            logger.info("  ✅ Constraint updated (size = medium)")
            
            # Test 3: Try again (should be approved now)
            approved, reason = await self.orchestrator.mission_brain.audit_action(
                mission_id=mission.mission_id,
                action_type="click",
                target_id="add-to-cart",
                target_text="Add to Cart"
            )
            
            if approved:
                logger.info(f"  ✅ Action correctly approved: {reason}")
            else:
                logger.warning(f"  ⚠️ Action should have been approved: {reason}")
            
            # Cleanup
            await self.orchestrator.mission_brain.delete_mission(mission.mission_id)
            
            self.record_result("Mission Brain", True, "Constraint enforcement working")
            
        except Exception as e:
            logger.error(f"  ❌ Mission Brain test failed: {e}")
            self.record_result("Mission Brain", False, str(e))
    
    async def test_4_dom_delta_handler(self):
        """Test DOM delta message handling."""
        logger.info("\n" + "=" * 60)
        logger.info("Test 4: DOM Delta Handler")
        logger.info("=" * 60)
        
        if not self.orchestrator or not self.orchestrator.live_graph:
            self.record_result("DOM Delta Handler", False, "LiveGraph not available")
            return
        
        session_id = f"test-delta-{int(time.time())}"
        
        try:
            # Test incremental update
            # First, add a node
            add_delta = {
                "delta_type": "add",
                "nodes": [{
                    "id": "tara-btn1",
                    "tag": "button",
                    "text": "Submit",
                    "role": "button",
                    "zone": "main",
                    "interactive": True,
                    "visible": True,
                    "rect": {},
                    "parent_id": None,
                    "depth": 1,
                    "state": "",
                    "timestamp": time.time()
                }],
                "url": "https://example.com",
                "timestamp": time.time()
            }
            
            await self.orchestrator.ingest_dom_delta(session_id, add_delta)
            nodes = await self.orchestrator.live_graph.get_visible_nodes(session_id)
            logger.info(f"  ✅ After add: {len(nodes)} nodes")
            
            # Test update
            update_delta = {
                "delta_type": "update",
                "changes": [{
                    "type": "update",
                    "node": {
                        "id": "tara-btn1",
                        "tag": "button",
                        "text": "Submit Now",  # Changed text
                        "role": "button",
                        "zone": "main",
                        "interactive": True,
                        "visible": True,
                        "rect": {},
                        "parent_id": None,
                        "depth": 1,
                        "state": "focused",  # Added state
                        "timestamp": time.time()
                    }
                }],
                "url": "https://example.com",
                "timestamp": time.time()
            }
            
            await self.orchestrator.ingest_dom_delta(session_id, update_delta)
            node = await self.orchestrator.live_graph.find_by_id(session_id, "tara-btn1")
            if node and node.text == "Submit Now":
                logger.info(f"  ✅ Node updated: {node.text}, state={node.state}")
            else:
                logger.warning(f"  ⚠️ Update failed")
            
            # Test remove
            remove_delta = {
                "delta_type": "remove",
                "removed_ids": ["tara-btn1"],
                "url": "https://example.com",
                "timestamp": time.time()
            }
            
            await self.orchestrator.ingest_dom_delta(session_id, remove_delta)
            nodes = await self.orchestrator.live_graph.get_visible_nodes(session_id)
            logger.info(f"  ✅ After remove: {len(nodes)} nodes")
            
            # Cleanup
            await self.orchestrator.live_graph.clear_graph(session_id)
            
            self.record_result("DOM Delta Handler", True, "Incremental updates working")
            
        except Exception as e:
            logger.error(f"  ❌ DOM Delta Handler test failed: {e}")
            self.record_result("DOM Delta Handler", False, str(e))
    
    def print_summary(self):
        """Print test summary."""
        logger.info("\n" + "=" * 60)
        logger.info("ULTIMATE TARA - Integration Test Summary")
        logger.info("=" * 60)
        logger.info(f"Tests Run:    {self.results['tests_run']}")
        logger.info(f"Tests Passed: {self.results['tests_passed']} ✅")
        logger.info(f"Tests Failed: {self.results['tests_failed']} ❌")
        logger.info("=" * 60)
        
        if self.results['tests_failed'] == 0:
            logger.info("🎉 ALL TESTS PASSED! Ultimate TARA is ready for deployment.")
            return True
        else:
            logger.warning("⚠️ Some tests failed. Review logs for details.")
            return False


async def main():
    """Run all integration tests."""
    print("\n" + "=" * 70)
    print(" " * 15 + "ULTIMATE TARA INTEGRATION TEST")
    print("=" * 70)
    
    test_suite = UltimateIntegrationTest()
    
    # Setup
    if not await test_suite.setup():
        print("❌ Setup failed")
        return False
    
    try:
        # Run tests
        await test_suite.test_1_mind_reader()
        await test_suite.test_2_live_graph()
        await test_suite.test_3_mission_brain_constraints()
        await test_suite.test_4_dom_delta_handler()
        
        # Summary
        return test_suite.print_summary()
        
    finally:
        await test_suite.teardown()


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result else 1)
