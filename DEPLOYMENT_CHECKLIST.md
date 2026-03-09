# TARA Visual Copilot - Critical Bug Fix Deployment

## Deployment Date
March 9, 2026

## Version
v2.1.0-critical-fixes

## Bugs Fixed

### P0 Critical (System Breaking)
- [x] Unbound variable `prefetched_nodes_for_gate`
- [x] Infinite loop on empty JSON
- [x] Frontend action history mismatch
- [x] Multi-action pipeline race condition

### P1 High Priority (Major Functionality)
- [x] Completion gate evidence rescue
- [x] Vision bootstrap override
- [x] PageIndex IDF cache rebuild
- [x] SPA navigation flag clear
- [x] Pipeline TTL alignment (1h→24h)
- [x] Vision API timeout (10s→25s)

### P2 Medium Priority (Degraded Experience)
- [x] Action ledger max size (10→20)
- [x] Mission context truncation (500→2000)

## Files Modified

### Backend (Python)
1. `rag-visual-copilot/visual_copilot/orchestration/plan_next_step_flow.py`
2. `rag-visual-copilot/visual_copilot/mission/last_mile.py`
3. `rag-visual-copilot/visual_copilot/orchestration/stages/session_stage.py`
4. `rag-visual-copilot/visual_copilot/orchestration/stages/page_index.py`
5. `rag-visual-copilot/visual_copilot/mission/screenshot_broker.py`
6. `orchestra_daytona.v2/core/pipeline_resume.py`
7. `orchestra_daytona.v2/core/action_ledger.py`

### Frontend (JavaScript)
1. `orchestra_daytona.v2/static/tara-ws.js`
2. `orchestra_daytona.v2/static/tara-executor.js`
3. `orchestra_daytona.v2/static/tara-phoenix.js`

## Pre-Deployment Checks

- [ ] All Python files pass `python3 -m py_compile`
- [ ] All JavaScript files pass `node --check`
- [ ] No new linting errors
- [ ] Redis connection tested
- [ ] Groq API connectivity verified
- [ ] WebSocket endpoints accessible

## Deployment Steps

### Step 1: Backend Deployment
```bash
# Stop existing services
docker-compose -f docker-compose.yml down

# Pull latest code
git pull origin main

# Build backend images
docker-compose -f docker-compose.yml build visual-copilot orchestra-daytona

# Start services
docker-compose -f docker-compose.yml up -d visual-copilot orchestra-daytona

# Verify health
curl http://localhost:4005/health
```

### Step 2: Frontend Deployment
```bash
# Copy updated static files
cp orchestra_daytona.v2/static/tara-*.js /path/to/web/server/static/

# Clear CDN cache (if applicable)
# [CDN-specific commands]

# Reload nginx
sudo nginx -s reload
```

### Step 3: Verification
```bash
# Test P0 fix: Unbound variable
curl -X POST http://localhost:4005/api/v1/plan_next_step \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test", "goal": "test goal", "step": 1}'

# Test P1 fix: Vision timeout
curl -X POST http://localhost:4005/api/v1/request_vision \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test", "reason": "test"}'

# Monitor logs
docker-compose logs -f visual-copilot | grep -E "(ERROR|WARNING|P0|P1)"
```

## Rollback Plan

If issues detected:
```bash
# Rollback to previous version
git checkout [PREVIOUS_COMMIT]
docker-compose -f docker-compose.yml down
docker-compose -f docker-compose.yml build
docker-compose -f docker-compose.yml up -d
```

## Monitoring

### Key Metrics to Watch
- Error rate (should decrease by >80%)
- Vision API success rate (should increase from 70% to >95%)
- Pipeline expiry events (should decrease to ~0)
- SPA navigation stalls (should decrease to ~0)
- Average mission completion time (should decrease by 30-40%)

### Alerting Thresholds
- Error rate > 5% → Page on-call
- Vision success < 90% → Investigate
- Pipeline expiry > 1/day → Check TTL

## Success Criteria

Deployment successful when:
- [ ] No P0 crashes for 24 hours
- [ ] No P1 failures for 24 hours
- [ ] Error rate < 2%
- [ ] Vision success rate > 95%
- [ ] User reports of "stuck" missions decrease by >50%

## Post-Deployment Tasks

- [ ] Update documentation
- [ ] Notify stakeholders
- [ ] Schedule P2/P3 bug fix sprint
- [ ] Review monitoring dashboards
- [ ] Collect user feedback
