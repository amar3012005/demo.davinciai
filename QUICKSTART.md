# 🎯 Quick Start - TARA Ultimate Local Deployment

## One-Command Start

```bash
cd /Users/amar/demo.davinciai
./start-local.sh
```

This will:
1. ✅ Create `.env` from `.env.local` (if not exists)
2. ✅ Start all Docker services
3. ✅ Check service health
4. ✅ Open browser to test page

---

## Manual Steps (Alternative)

### 1. Setup Environment
```bash
cd /Users/amar/demo.davinciai
cp .env.local .env
```

### 2. Start Docker
```bash
docker-compose -f docker-compose.local.yml up -d
```

### 3. Check Status
```bash
docker-compose -f docker-compose.local.yml ps
```

### 4. Open Browser
```
http://localhost:8004
```

---

## What Changed from Production?

### WebSocket URLs

| Before (Production) | After (Local) |
|---------------------|---------------|
| `wss://demo.davinciai.eu:8443/ws` | `ws://localhost:8004/ws` |
| SSL enabled | No SSL |
| Hardcoded | Configurable |

### Widget Configuration

**Before:**
```javascript
const DEFAULTS = {
    wsUrl: 'wss://demo.davinciai.eu:8443/ws'  // Hardcoded
};
```

**After:**
```javascript
// Auto-detects from:
// 1. window.TARA_ENV.WS_URL
// 2. Script tag data attribute
// 3. Defaults to production
const ENV_CONFIG = getEnvConfig() || {};
const DEFAULTS = {
    wsUrl: ENV_CONFIG.wsUrl || 'wss://demo.davinciai.eu:8443/ws'
};
```

---

## Test It Works

### Browser Console (F12)
```javascript
// Should show: ws://localhost:8004/ws
console.log(window.TARA_CONFIG.wsUrl);

// Should show: function TaraSensor(...)
console.log(window.TaraSensor);
```

### Server Logs
```bash
docker-compose -f docker-compose.local.yml logs orchestrator | grep "✅"
```

Expected:
```
✅ Redis connected: redis://redis:6379/0
✅ Qdrant connected: http://...
👁️ DOM Delta: Full scan (XX nodes)
```

---

## Services Running

| Service | Port | Status |
|---------|------|--------|
| Orchestrator | 8004 | http://localhost:8004 |
| RAG | 8003 | http://localhost:8003 |
| STT | 8002 | http://localhost:8002 |
| TTS | 8000 | http://localhost:8000 |
| Redis | 6379 | ✅ Included |
| Qdrant | 6333 | ☁️ Cloud (or local with profile) |

---

## Common Issues

### "Cannot connect to Docker"
```bash
# Start Docker Desktop
open -a Docker
```

### "Port already in use"
```bash
# Stop conflicting services
docker-compose -f docker-compose.local.yml down

# Restart
docker-compose -f docker-compose.local.yml up -d
```

### "Widget shows production URL"
```html
<!-- Add before tara-widget.js -->
<script>
    window.TARA_ENV = { WS_URL: 'ws://localhost:8004/ws' };
</script>
```

---

## Next Steps

1. ✅ **Test Basic Functionality**
   - Click TARA orb
   - Check console for `✅ TaraSensor initialized`
   - Speak or type a command

2. ✅ **Run Integration Tests**
   ```bash
   cd rag-daytona.v2
   python3 test_ultimate_integration.py
   ```

3. ✅ **Review Logs**
   ```bash
   docker-compose -f docker-compose.local.yml logs -f
   ```

---

## Documentation

- **LOCAL_DEPLOYMENT_GUIDE.md** - Complete guide
- **ULTIMATE_ARCHITECTURE.md** - Architecture details
- **DEPLOYMENT_CHECKLIST.md** - Production checklist

---

## Need Help?

```bash
# View logs
docker-compose -f docker-compose.local.yml logs -f orchestrator

# Restart services
docker-compose -f docker-compose.local.yml restart

# Stop all
docker-compose -f docker-compose.local.yml down

# Clean start
docker-compose -f docker-compose.local.yml down -v
docker-compose -f docker-compose.local.yml up -d
```

---

**Ready? Run `./start-local.sh` and go! 🚀**
