# 🚀 TARA Ultimate - Local Docker Deployment Guide

## Quick Start (5 Minutes)

### Step 1: Copy Environment File
```bash
cd /Users/amar/demo.davinciai
cp .env.local .env
```

### Step 2: Start Docker Compose
```bash
# Start all services (Redis, RAG, STT, TTS, Orchestrator)
docker-compose -f docker-compose.local.yml up -d

# Optional: Include local Qdrant (if not using cloud)
docker-compose -f docker-compose.local.yml --profile with-qdrant up -d
```

### Step 3: Check Services
```bash
# View running containers
docker-compose -f docker-compose.local.yml ps

# View logs
docker-compose -f docker-compose.local.yml logs -f orchestrator
```

### Step 4: Open in Browser
```
http://localhost:8004
```

### Step 5: Test TARA Widget
1. Click the TARA orb (bottom-right corner)
2. Check browser console for:
   ```
   ✅ TaraSensor initialized
   👁️ TaraSensor started, watching DOM...
   ```
3. Check server logs for:
   ```
   👁️ DOM Delta: Full scan (XX nodes)
   ```

---

## 📋 What's Configured

### WebSocket URLs (Local vs Production)

| Environment | WebSocket URL | SSL |
|-------------|---------------|-----|
| **Local** | `ws://localhost:8004/ws` | ❌ No |
| **Production** | `wss://demo.davinciai.eu:8443/ws` | ✅ Yes |

### Widget Configuration

The widget now **automatically detects** the WebSocket URL:

1. **From `window.TARA_ENV`** (set by backend)
2. **From script tag data attribute**
3. **Defaults to production** (if neither available)

```javascript
// In your HTML, configure like this:
<script>
    window.TARA_ENV = {
        WS_URL: 'ws://localhost:8004/ws'  // Local
        // WS_URL: 'wss://demo.davinciai.eu:8443/ws'  // Production
    };
</script>
<script src="tara-widget.js"></script>
```

Or with data attribute:
```html
<script src="tara-widget.js" data-ws-url="ws://localhost:8004/ws"></script>
```

---

## 🔧 Configuration Options

### Environment Variables (.env file)

```bash
# ═══════════════════════════════════════════════════════════
# LLM Configuration
# ═══════════════════════════════════════════════════════════
LLM_API_KEY=your_groq_api_key
MIND_READER_MODEL=llama-3.1-8b-instant

# ═══════════════════════════════════════════════════════════
# Qdrant (Hive Mind)
# ═══════════════════════════════════════════════════════════
# Cloud Qdrant (recommended)
QDRANT_URL=https://your-cloud-qdrant-url:6333
QDRANT_API_KEY=your-api-key

# OR Local Qdrant
# QDRANT_URL=http://localhost:6333
# QDRANT_API_KEY=

# ═══════════════════════════════════════════════════════════
# Ultimate TARA Feature Flags
# ═══════════════════════════════════════════════════════════
USE_NEW_DETECTIVE=true      # Enable semantic detective
USE_MISSION_BRAIN=true       # Enable constraint enforcement
USE_LIVE_GRAPH=true          # Enable Redis DOM mirror
USE_HIVE_INTERFACE=true      # Enable Qdrant retrieval
```

### Docker Compose Profiles

```bash
# Standard setup (uses cloud Qdrant)
docker-compose -f docker-compose.local.yml up -d

# With local Qdrant
docker-compose -f docker-compose.local.yml --profile with-qdrant up -d
```

---

## 🧪 Testing

### 1. Test Page
Open: `http://localhost:8004/static/test-ultimate.html`

This shows:
- Service status
- WebSocket configuration
- TaraSensor initialization

### 2. Integration Tests
```bash
cd rag-daytona.v2
python3 test_ultimate_integration.py
```

Expected output:
```
✅ Redis connected: redis://redis:6379/0
✅ Qdrant connected: http://...
🧠 Mind Reader: 'Buy a white shirt' → purchase
✅ Action correctly blocked: Cannot Add to Cart until size is selected
🎉 ALL TESTS PASSED!
```

### 3. Browser Console Tests
```javascript
// Check if TaraSensor is available
console.log(window.TaraSensor);  // Should show class

// Check widget configuration
const widget = new TaraWidget();
console.log(widget.config.wsUrl);  // Should show ws://localhost:8004/ws

// Get sensor stats (after initialization)
if (widget.sensor) {
    console.log(widget.sensor.getStats());
}
```

---

## 📊 Service Ports

| Service | Port | URL |
|---------|------|-----|
| Orchestrator | 8004 | http://localhost:8004 |
| RAG | 8003 | http://localhost:8003 |
| STT | 8002 | http://localhost:8002 |
| TTS | 8000 | http://localhost:8000 |
| Redis | 6379 | http://localhost:6379 |
| Qdrant (local) | 6333 | http://localhost:6333 |

---

## 🔍 Troubleshooting

### Issue: Widget shows production URL
**Solution:** Set `window.TARA_ENV` before loading widget:
```html
<script>
    window.TARA_ENV = { WS_URL: 'ws://localhost:8004/ws' };
</script>
<script src="tara-widget.js"></script>
```

### Issue: "Cannot connect to Redis"
**Solution:** 
```bash
# Check Redis is running
docker-compose -f docker-compose.local.yml ps redis

# Restart Redis
docker-compose -f docker-compose.local.yml restart redis

# Check logs
docker-compose -f docker-compose.local.yml logs redis
```

### Issue: "Qdrant not available"
**Solution:**
1. Use cloud Qdrant (set `QDRANT_URL` in .env)
2. OR start local Qdrant:
   ```bash
   docker-compose -f docker-compose.local.yml --profile with-qdrant up qdrant
   ```

### Issue: TaraSensor not initializing
**Solution:**
1. Check browser console for errors
2. Verify scripts load in correct order:
   ```html
   <script src="tara-widget.js"></script>
   <script src="tara-sensor.js"></script>
   <script src="tara-widget-ultimate-integration.js"></script>
   ```
3. Check orchestrator logs:
   ```bash
   docker-compose -f docker-compose.local.yml logs -f orchestrator
   ```

---

## 📝 Deployment Checklist

- [ ] Copy `.env.local` to `.env`
- [ ] Update API keys in `.env`
- [ ] Start Docker: `docker-compose -f docker-compose.local.yml up -d`
- [ ] Check services: `docker-compose -f docker-compose.local.yml ps`
- [ ] Open browser: `http://localhost:8004`
- [ ] Click TARA orb
- [ ] Check console for `✅ TaraSensor initialized`
- [ ] Run integration tests: `python3 test_ultimate_integration.py`

---

## 🎯 Production Deployment

When ready for production:

1. **Update .env**:
   ```bash
   WIDGET_WS_URL=wss://demo.davinciai.eu:8443/ws
   PUBLIC_URL=https://demo.davinciai.eu:8443
   ORCHESTRATOR_WS_URL=wss://demo.davinciai.eu:8443/ws
   ```

2. **Use production docker-compose**:
   ```bash
   docker-compose -f docker-compose.yml up -d
   ```

3. **Enable SSL** (already configured in production docker-compose.yml)

---

## 📚 Additional Resources

- `ULTIMATE_ARCHITECTURE.md` - Architecture overview
- `DEPLOYMENT_CHECKLIST.md` - Complete deployment checklist
- `ULTIMATE_INTEGRATION_GUIDE.md` - Integration details
- `test-ultimate.html` - Visual test page

---

## ✅ Success Indicators

### Browser Console:
```
🔧 TARA Widget Ultimate Integration loaded
✅ TaraWidget and TaraSensor available
👁️ TaraSensor initialized
👁️ TaraSensor started, watching DOM...
📸 Full scan: 45 nodes in 12.34ms
```

### Server Logs:
```
✅ Redis connected: redis://redis:6379/0
✅ Qdrant connected: http://...
🧠 Mind Reader initialized
🔍 SemanticDetective initialized
🧠 MissionBrain initialized
👁️ DOM Delta: Full scan (45 nodes)
```

**You're ready to go! 🚀**
