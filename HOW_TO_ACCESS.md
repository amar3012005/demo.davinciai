# 🎯 How to Access TARA Ultimate Locally

## ✅ Correct URLs (After Starting Docker)

### Main Application
```
http://localhost:8004
```
This shows the orchestrator health check.

### Test Page (NEW)
```
http://localhost:8004/static/test-ultimate.html
```
This is the test page for Ultimate TARA.

### Client Interface
```
http://localhost:8004/client
```
This is the main client interface (if available).

---

## 📁 Main Widget File Location

**Primary File:**
```
/Users/amar/demo.davinciai/orchestra_daytona.v2/static/tara-widget.js
```

This is the **main production widget** that handles:
- Visual Co-Pilot mode
- Audio streaming
- DOM collection
- WebSocket communication

**Supporting Files:**
- `tara-sensor.js` - NEW delta streaming (Ultimate TARA)
- `tara-widget-ultimate-integration.js` - Integration layer
- `tara-widget_old.js` - Backup (not used)

---

## 🚀 Step-by-Step to Test

### 1. Start Docker
```bash
cd /Users/amar/demo.davinciai
./start-local.sh
```

### 2. Wait for Services (30 seconds)
The script will show:
```
✅ Orchestrator is ready (port 8004)
✅ RAG service is ready (port 8003)
```

### 3. Open Test Page
```
http://localhost:8004/static/test-ultimate.html
```

You should see:
- ✅ Status indicators
- 📊 Widget configuration
- 🚀 Quick start instructions

### 4. Open Browser Console (F12)
Look for:
```
🔧 TARA Widget Ultimate Integration loaded
✅ TaraWidget and TaraSensor available
👁️ TaraSensor initialized
```

### 5. Click TARA Orb
Should appear in bottom-right corner of any page.

---

## 🔍 Troubleshooting

### Page Shows "Cannot connect to orchestrator"
**Wait longer** - Services take 30-60 seconds to start.

Check status:
```bash
docker-compose -f docker-compose.local.yml ps
```

All should show `Up` status.

### 404 Not Found
Static files might not be mounted. Check logs:
```bash
docker-compose -f docker-compose.local.yml logs orchestrator | grep "Static"
```

Should show:
```
✅ Static files mounted from /app/static
```

### Widget Shows Production URL
The widget is using the default production URL. Fix it:

**Option 1: Set in HTML** (add before widget scripts):
```html
<script>
    window.TARA_ENV = {
        WS_URL: 'ws://localhost:8004/ws'
    };
</script>
```

**Option 2: Use data attribute**:
```html
<script src="/static/tara-widget.js" data-ws-url="ws://localhost:8004/ws"></script>
```

---

## 📊 Quick Status Check

```bash
# Check all services
docker-compose -f docker-compose.local.yml ps

# Expected output:
# NAME                    STATUS
# orchestrator-local      Up
# rag-local               Up
# redis-local             Up
# stt-local               Up
# tts-local               Up
```

---

## 🎯 What to Test

1. **Open Test Page**: `http://localhost:8004/static/test-ultimate.html`
2. **Check Status**: Should show "✅ Orchestrator running"
3. **Open Console**: Should show TaraSensor logs
4. **Click Orb**: Should activate Visual Co-Pilot
5. **Speak/Type**: Should send commands

---

## 📝 File Summary

| File | Purpose | Location |
|------|---------|----------|
| **tara-widget.js** | Main widget | `/static/tara-widget.js` |
| **tara-sensor.js** | Delta streaming | `/static/tara-sensor.js` |
| **test-ultimate.html** | Test page | `/static/test-ultimate.html` |
| **docker-compose.local.yml** | Local Docker config | Project root |
| **.env.local** | Environment template | Project root |

---

## ✅ Success Checklist

- [ ] Docker services running (`docker-compose ps`)
- [ ] Test page loads (`http://localhost:8004/static/test-ultimate.html`)
- [ ] Shows "✅ Orchestrator running"
- [ ] Browser console shows TaraSensor initialized
- [ ] TARA orb visible (bottom-right)
- [ ] Can click orb and see logs

**If all checked, you're good to go! 🚀**
