# Ultimate TARA Integration Guide

## LLM Usage Summary

| Module | Model | Purpose | Latency | When Used |
|--------|-------|---------|---------|-----------|
| **Mind Reader** | `llama-3.1-8b-instant` | Intent parsing (user input → schema) | ~100ms | Every user request |
| **Semantic Detective** | `all-MiniLM-L6-v2` (embedding model) | Semantic similarity scoring | ~10ms (local) | Every element investigation |
| **Legacy Orchestrator** | `llama-3.1-8b-instant` / `openai/gpt-oss-120b` | Action decision making | ~500ms-2s | Every DOM interaction |

### Key Points:
1. **Mind Reader** uses **Llama-3.1-8B** (fast, cheap) for intent parsing
2. **Semantic Detective** uses **Sentence Transformers** (local, no API call)
3. **Legacy Visual Orchestrator** continues using existing 8B/120B models

---

## Qdrant Configuration

The system uses the **QDRANT_URL** environment variable:

```bash
# Production Qdrant URL
QDRANT_URL='http://qdrant-n80wo80os08gswko4040wo8g.116.202.24.69.sslip.io:6333'
QDRANT_API_KEY='WAkhOeXiD3DShev81qxn5PYKpQ9t6ufb'
QDRANT_COLLECTION='tara_hive'  # New collection for Ultimate TARA
```

### Qdrant Document Types:
1. **Strategy_Sequence** - High-level navigation plans
2. **Visual_Hint** - Element selectors and patterns  
3. **Website_Map** - Legacy site maps (backward compatible)

---

## Phase 5.1: Update visual_orchestrator.py

### Option A: Wrapper Approach (Recommended)

Use `visual_orchestrator_ultimate.py` as a wrapper that:
- Initializes all new modules
- Maintains backward compatibility with legacy `VisualOrchestrator`
- Provides feature flags for gradual rollout

```python
# In your WebSocket handler or main app
from visual_orchestrator_ultimate import create_ultimate_orchestrator

# Initialize with Qdrant URL from environment
ultimate_orchestrator = create_ultimate_orchestrator(
    groq_provider=groq,
    qdrant_url=os.getenv("QDRANT_URL"),
    qdrant_api_key=os.getenv("QDRANT_API_KEY"),
    redis_url=os.getenv("REDIS_URL", "redis://localhost:6379")
)

# Use new architecture
result = await ultimate_orchestrator.execute_with_new_architecture(
    session_id=session_id,
    user_input="Export my API usage",
    current_url="https://groq.com/dashboard"
)
```

### Option B: Direct Integration

Add new modules to existing `VisualOrchestrator.__init__()`:

```python
# In visual_orchestrator.py VisualOrchestrator class

def __init__(self, groq_provider: GroqProvider, qdrant_client=None, embeddings=None, redis_client=None):
    self.groq = groq_provider
    self.qdrant = qdrant_client
    self.embeddings = embeddings
    self.redis = redis_client
    
    # ... existing initialization ...
    
    # NEW: Initialize Ultimate modules
    self._init_ultimate_modules()

def _init_ultimate_modules(self):
    """Initialize Ultimate TARA modules."""
    
    # 1. Mind Reader
    try:
        from mind_reader import MindReader
        self.mind_reader = MindReader(self.groq)
        logger.info("✅ MindReader initialized")
    except Exception as e:
        self.mind_reader = None
        logger.warning(f"MindReader not available: {e}")
    
    # 2. Hive Interface
    try:
        from hive_interface import HiveInterface
        self.hive_interface = HiveInterface(
            qdrant_client=self.qdrant,
            embeddings=self.embeddings,
            redis_client=self.redis,
            collection_name=os.getenv("QDRANT_COLLECTION", "tara_hive")
        )
        logger.info("✅ HiveInterface initialized")
    except Exception as e:
        self.hive_interface = None
        logger.warning(f"HiveInterface not available: {e}")
    
    # 3. Live Graph
    try:
        from live_graph import LiveGraph
        self.live_graph = LiveGraph(self.redis) if self.redis else None
        logger.info("✅ LiveGraph initialized") if self.live_graph else None
    except Exception as e:
        self.live_graph = None
        logger.warning(f"LiveGraph not available: {e}")
    
    # 4. Semantic Detective
    try:
        from semantic_detective import SemanticDetective
        self.semantic_detective = SemanticDetective(self.live_graph) if self.live_graph else None
        logger.info("✅ SemanticDetective initialized") if self.semantic_detective else None
    except Exception as e:
        self.semantic_detective = None
        logger.warning(f"SemanticDetective not available: {e}")
    
    # 5. Mission Brain
    try:
        from mission_brain import MissionBrain
        self.mission_brain = MissionBrain(self.redis, self.hive_interface) if self.redis else None
        logger.info("✅ MissionBrain initialized") if self.mission_brain else None
    except Exception as e:
        self.mission_brain = None
        logger.warning(f"MissionBrain not available: {e}")
```

---

## Phase 5.2: Update tara-widget.js

Initialize `TaraSensor` in the widget:

```javascript
// In tara-widget.js, find startVisualCopilot() method

startVisualCopilot(sessionId, interactionMode = 'interactive') {
    // ... existing initialization ...
    
    // NEW: Initialize TaraSensor
    if (window.TaraSensor) {
        this.sensor = new TaraSensor(this.ws, {
            sendFullScanOnInit: true,
            debounceMs: 150,
            maxBatchSize: 50
        });
        this.sensor.start();
        console.log('✅ TaraSensor initialized');
    } else {
        console.warn('⚠️ TaraSensor not available, using legacy DOM capture');
    }
    
    // ... rest of existing code ...
}
```

---

## Phase 5.3: WebSocket Handler for dom_delta

Add handler in your WebSocket endpoint:

```python
# In your WebSocket handler (e.g., ws_handler.py or app.py)

@websocket("/ws")
async def websocket_handler(websocket: WebSocket, session_id: str):
    await websocket.accept()
    
    # Get orchestrator (from app state or create new)
    orchestrator = app.state.ultimate_orchestrator
    
    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")
            
            # NEW: Handle dom_delta messages
            if msg_type == "dom_delta":
                if orchestrator and orchestrator.live_graph:
                    success = await orchestrator.ingest_dom_delta(
                        session_id=session_id,
                        delta=message
                    )
                    if not success:
                        logger.warning("Failed to ingest DOM delta")
                continue
            
            # Existing message handling...
            elif msg_type == "user_input":
                # Use new architecture
                if orchestrator and orchestrator.config.use_new_detective:
                    result = await orchestrator.execute_with_new_architecture(
                        session_id=session_id,
                        user_input=message.get("text"),
                        current_url=message.get("url", "")
                    )
                else:
                    # Fallback to legacy
                    result = await legacy_execute(...)
                
                await websocket.send_json(result)
    
    except WebSocketDisconnect:
        logger.info(f"Session {session_id} disconnected")
```

---

## Feature Flags

Control rollout with environment variables:

```bash
# Enable/disable new modules
USE_NEW_DETECTIVE=false          # Use semantic_detective (default: false)
USE_MISSION_BRAIN=false          # Use mission_brain (default: false)
USE_LIVE_GRAPH=true              # Use live_graph (default: true if Redis available)
USE_HIVE_INTERFACE=true          # Use hive_interface (default: true if Qdrant available)

# Qdrant configuration
QDRANT_URL='http://qdrant-n80wo80os08gswko4040wo8g.116.202.24.69.sslip.io:6333'
QDRANT_API_KEY='WAkhOeXiD3DShev81qxn5PYKpQ9t6ufb'
QDRANT_COLLECTION='tara_hive'

# Redis configuration
REDIS_URL='redis://localhost:6379'

# Model configuration
MIND_READER_MODEL='llama-3.1-8b-instant'
EMBEDDING_MODEL='all-MiniLM-L6-v2'
```

---

## Testing the Integration

### 1. Test Mind Reader
```python
from mind_reader import MindReader

mind_reader = MindReader(groq_provider)
schema = await mind_reader.translate(
    user_input="Find my API usage for last month",
    current_url="https://groq.com"
)
print(f"Action: {schema.action.value}")
print(f"Constraints: {schema.constraints}")
```

### 2. Test Hive Interface
```python
from hive_interface import HiveInterface
from tara_models import TacticalSchema, ActionIntent

hive = HiveInterface(qdrant_client, embeddings, redis_client)
schema = TacticalSchema(
    action=ActionIntent.EXTRACTION,
    target_entity="API usage",
    domain="groq.com",
    constraints={}
)
response = await hive.retrieve(schema)
print(f"Strategy: {response.strategy.sequence if response.strategy else 'None'}")
print(f"Hints: {len(response.visual_hints)}")
```

### 3. Test Live Graph
```python
from live_graph import LiveGraph

live_graph = LiveGraph(redis_client)

# Simulate delta from browser
delta = {
    "delta_type": "full_scan",
    "nodes": [
        {
            "id": "tara-test123",
            "tag": "button",
            "text": "Export",
            "role": "button",
            "zone": "toolbar",
            "interactive": True,
            "visible": True,
            "rect": {"x": 100, "y": 200, "w": 80, "h": 40},
            "parent_id": None,
            "depth": 2,
            "state": "",
            "timestamp": time.time()
        }
    ],
    "url": "https://groq.com",
    "timestamp": time.time()
}

await live_graph.ingest_delta("session-123", delta)
nodes = await live_graph.get_visible_nodes("session-123")
print(f"Nodes in graph: {len(nodes)}")
```

### 4. Test Mission Brain Constraint Enforcement
```python
from mission_brain import MissionBrain
from tara_models import TacticalSchema, ActionIntent, ConstraintStatus

brain = MissionBrain(redis_client, hive_interface)

# Create mission with missing constraint
schema = TacticalSchema(
    action=ActionIntent.PURCHASE,
    target_entity="shirt",
    domain="shop.com",
    constraints={"color": "white", "size": None}  # size is MISSING
)

mission = await brain.create_mission("session-123", schema)

# Try to add to cart without selecting size
approved, reason = await brain.audit_action(
    mission_id=mission.mission_id,
    action_type="click",
    target_id="add-to-cart",
    target_text="Add to Cart"
)

print(f"Approved: {approved}")
print(f"Reason: {reason}")
# Output: Approved: False
#         Reason: Cannot Add to Cart until size is selected
```

---

## Migration Checklist

- [ ] **Phase 5.1**: `visual_orchestrator_ultimate.py` created ✅
- [ ] **Phase 5.2**: Update `tara-widget.js` to initialize TaraSensor
- [ ] **Phase 5.3**: Add WebSocket handler for `dom_delta` messages
- [ ] **Phase 5.4**: Test end-to-end flow with real user request
- [ ] **Phase 6**: Enable feature flags gradually

---

## Rollback Plan

If issues occur:

1. **Disable new modules** via feature flags:
   ```bash
   USE_NEW_DETECTIVE=false
   USE_MISSION_BRAIN=false
   ```

2. **Fallback to legacy** - `visual_orchestrator_ultimate.py` automatically falls back to legacy `VisualOrchestrator` if new modules fail

3. **Redis/Qdrant unavailable** - All modules degrade gracefully:
   - Redis down → In-memory state
   - Qdrant down → Pure semantic scoring
   - LLM timeout → Heuristic fallback

---

## Production Deployment

### Docker Compose Configuration

```yaml
services:
  tara-app:
    environment:
      # Qdrant (Hive Mind)
      - QDRANT_URL=http://qdrant:6333
      - QDRANT_API_KEY=${QDRANT_API_KEY}
      - QDRANT_COLLECTION=tara_hive
      
      # Redis (Live Graph + State)
      - REDIS_URL=redis://redis:6379
      
      # Feature Flags
      - USE_NEW_DETECTIVE=true
      - USE_MISSION_BRAIN=true
      - USE_LIVE_GRAPH=true
      
      # LLM Models
      - MIND_READER_MODEL=llama-3.1-8b-instant
      - EMBEDDING_MODEL=all-MiniLM-L6-v2
  
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
  
  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
    volumes:
      - qdrant_storage:/qdrant/storage

volumes:
  qdrant_storage:
```

---

## Next Steps

1. **Test locally** with the integration tests above
2. **Deploy to staging** with feature flags disabled
3. **Enable gradually** for internal testing
4. **Monitor metrics**:
   - Mind Reader latency
   - Hive Interface hit rate
   - Mission Brain constraint blocks
   - Semantic Detective accuracy
5. **Full rollout** when metrics are stable
