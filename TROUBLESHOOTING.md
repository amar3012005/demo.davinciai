# 🔧 Troubleshooting Guide - TARA Ultimate Local

## Issue: Test Page Not Loading (404)

### Solution 1: Check if Static Files are Mounted

```bash
# Check orchestrator logs
docker-compose -f docker-compose.local.yml logs orchestrator | grep -i "static"
```

**Expected output:**
```
✅ Static files mounted from /app/static
```

**If you see "⚠️ Static directory NOT found":**
The static folder might not be copied into the container.

**Fix:** Rebuild the container
```bash
docker-compose -f docker-compose.local.yml build orchestrator
docker-compose -f docker-compose.local.yml up -d orchestrator
```

---

### Solution 2: Check File Exists in Container

```bash
# Enter the container
docker exec -it orchestrator-local bash

# Check if files exist
ls -la /app/static/

# Should show:
# tara-widget.js
# tara-sensor.js
# test-ultimate.html
# etc.
```

**If files are missing:**
The Docker build didn't copy them. Check if files exist in source:
```bash
ls -la /Users/amar/demo.davinciai/orchestra_daytona.v2/static/
```

---

### Solution 3: Direct File Access Test

Try accessing files directly:

1. **Widget JS**: `http://localhost:8004/static/tara-widget.js`
2. **Sensor JS**: `http://localhost:8004/static/tara-sensor.js`
3. **Test Page**: `http://localhost:8004/static/test-ultimate.html`

If these return 404, static files aren't being served.

**Fix:** Check app.py mounting code is executing.

---

## Issue: "Cannot connect to orchestrator"

### Check Service Status

```bash
docker-compose -f docker-compose.local.yml ps
```

**All services should show "Up"**

If orchestrator shows "Exited" or "Restarting":

```bash
# Check logs
docker-compose -f docker-compose.local.yml logs orchestrator

# Common errors:
# - Redis connection failed → Check Redis is running
# - Port already in use → Stop other services using port 8004
# - Import error → Rebuild container
```

### Restart Services

```bash
# Restart all
docker-compose -f docker-compose.local.yml restart

# Or just orchestrator
docker-compose -f docker-compose.local.yml restart orchestrator
```

---

## Issue: Widget Shows Production URL

### Check Widget Configuration

The widget auto-detects URL in this order:

1. `window.TARA_ENV.WS_URL`
2. Script tag `data-ws-url` attribute
3. Default: `wss://demo.davinciai.eu:8443/ws`

### Fix Option 1: Set window.TARA_ENV

In your HTML page, add BEFORE loading tara-widget.js:

```html
<script>
    window.TARA_ENV = {
        WS_URL: 'ws://localhost:8004/ws'
    };
</script>
<script src="/static/tara-widget.js"></script>
```

### Fix Option 2: Use Data Attribute

```html
<script src="/static/tara-widget.js" data-ws-url="ws://localhost:8004/ws"></script>
```

### Fix Option 3: Edit Default in tara-widget.js

Open `/Users/amar/demo.davinciai/orchestra_daytona.v2/static/tara-widget.js`

Find line ~47:
```javascript
wsUrl: ENV_CONFIG.wsUrl || 'wss://demo.davinciai.eu:8443/ws',
```

Change to:
```javascript
wsUrl: ENV_CONFIG.wsUrl || 'ws://localhost:8004/ws',
```

**Note:** This changes the default for all deployments.

---

## Issue: TaraSensor Not Initializing

### Check Browser Console

Should show:
```
✅ TaraWidget and TaraSensor available
👁️ TaraSensor initialized
```

**If shows "TaraSensor not available":**

1. Check tara-sensor.js loaded:
   ```
   http://localhost:8004/static/tara-sensor.js
   ```

2. Check load order in HTML:
   ```html
   <script src="/static/tara-widget.js"></script>
   <script src="/static/tara-sensor.js"></script>
   <script src="/static/tara-widget-ultimate-integration.js"></script>
   ```

### Check Integration Script

The integration script (`tara-widget-ultimate-integration.js`) should override `startVisualCopilot`.

Check browser console for:
```
🔧 TARA Widget Ultimate Integration loaded
```

---

## Issue: DOM Deltas Not Showing in Logs

### Check Server Logs

```bash
docker-compose -f docker-compose.local.yml logs -f orchestrator | grep "DOM"
```

**Expected:**
```
👁️ DOM Delta: Full scan (45 nodes)
👁️ DOM Delta: Update (+2 ~1 -0)
```

**If not showing:**

1. **Check WebSocket connection**
   - Open browser DevTools → Network → WS
   - Should show connection to `ws://localhost:8004/ws`
   - Status should be "101 Switching Protocols"

2. **Check message type**
   - In browser console, add logging:
   ```javascript
   const originalSend = WebSocket.prototype.send;
   WebSocket.prototype.send = function(data) {
       if (data.includes('dom_delta')) {
           console.log('📤 Sending dom_delta:', JSON.parse(data));
       }
       return originalSend.call(this, data);
   };
   ```

3. **Check ws_handler.py**
   - The handler should have `_handle_dom_delta` method
   - Check logs for "Unknown message type: dom_delta"
   - If seen, the handler wasn't updated

---

## Issue: Services Won't Start

### Port Conflicts

```bash
# Check what's using ports
lsof -i :8004
lsof -i :8003
lsof -i :8002
lsof -i :8000
lsof -i :6379
```

**Fix:** Stop conflicting services or change ports in docker-compose.local.yml

### Redis Connection Failed

```bash
# Check Redis
docker-compose -f docker-compose.local.yml logs redis

# Restart Redis
docker-compose -f docker-compose.local.yml restart redis

# Check connectivity from orchestrator
docker exec orchestrator-local redis-cli -h redis ping
# Should return: PONG
```

### Qdrant Connection Failed

```bash
# If using cloud Qdrant, check internet
docker exec orchestrator-local curl -I https://google.com

# If using local Qdrant
docker-compose -f docker-compose.local.yml --profile with-qdrant up qdrant
```

---

## Issue: Build Errors

### "Cannot find module"

```bash
# Rebuild all containers
docker-compose -f docker-compose.local.yml build --no-cache

# Restart
docker-compose -f docker-compose.local.yml up -d
```

### "Network test failed"

Docker can't reach PyPI. Check Docker network:

```bash
docker exec orchestrator-local curl -I https://pypi.org/
```

**Fix:** Check Docker Desktop network settings or proxy configuration.

---

## Quick Diagnostic Commands

```bash
# 1. Check all services
docker-compose -f docker-compose.local.yml ps

# 2. Check logs for errors
docker-compose -f docker-compose.local.yml logs --tail=100

# 3. Check specific service
docker-compose -f docker-compose.local.yml logs orchestrator | tail -50

# 4. Restart everything
docker-compose -f docker-compose.local.yml down
docker-compose -f docker-compose.local.yml up -d

# 5. Clean rebuild
docker-compose -f docker-compose.local.yml down -v
docker-compose -f docker-compose.local.yml build --no-cache
docker-compose -f docker-compose.local.yml up -d
```

---

## Still Having Issues?

### Enable Debug Logging

Edit `.env`:
```bash
LOG_LEVEL=DEBUG
```

Then restart:
```bash
docker-compose -f docker-compose.local.yml restart orchestrator
```

### Check File Permissions

```bash
# Ensure files are readable
chmod 644 /Users/amar/demo.davinciai/orchestra_daytona.v2/static/*.js
chmod 644 /Users/amar/demo.davinciai/orchestra_daytona.v2/static/*.html
```

### Verify Environment

```bash
# Check .env was loaded
docker-compose -f docker-compose.local.yml config | grep -A 5 "environment"
```

---

## Success Indicators

✅ **All services Up:**
```
NAME                    STATUS
orchestrator-local      Up
rag-local               Up
redis-local             Up
stt-local               Up
tts-local               Up
```

✅ **Orchestrator logs:**
```
✅ Static files mounted from /app/static
✅ Redis connected: redis://redis:6379/0
✅ Qdrant connected: http://...
🧠 Mind Reader initialized
```

✅ **Browser console:**
```
✅ TaraWidget and TaraSensor available
👁️ TaraSensor initialized
👁️ TaraSensor started, watching DOM...
```

✅ **Test page loads:**
```
http://localhost:8004/static/test-ultimate.html
Shows: "✅ Orchestrator running on port 8004"
```

**If all ✅, you're good! 🚀**
