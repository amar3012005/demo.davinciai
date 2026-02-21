#!/bin/bash

# ═══════════════════════════════════════════════════════════
# TARA Ultimate - Local Development Startup Script
# ═══════════════════════════════════════════════════════════
# Usage: ./start-local.sh
#
# This script:
# 1. Copies .env.local to .env (if not exists)
# 2. Starts all Docker services
# 3. Shows service status
# 4. Opens browser to test page

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   TARA Ultimate - Local Development Setup               ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Step 1: Check .env file
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠️  .env file not found${NC}"
    echo "   Copying .env.local to .env..."
    cp .env.local .env
    echo -e "${GREEN}✅ Created .env from .env.local${NC}"
    echo ""
    echo -e "${YELLOW}📝 Note: Please review and update API keys in .env file${NC}"
    echo ""
else
    echo -e "${GREEN}✅ .env file found${NC}"
fi

# Step 2: Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker is not installed${NC}"
    exit 1
fi

if ! docker info &> /dev/null; then
    echo -e "${RED}❌ Docker is not running${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Docker is running${NC}"
echo ""

# Step 3: Start services
echo -e "${BLUE}🚀 Starting Docker services...${NC}"
echo ""

docker-compose -f docker-compose.local.yml up -d

echo ""
echo -e "${GREEN}✅ All services started${NC}"
echo ""

# Step 4: Show status
echo -e "${BLUE}📊 Service Status:${NC}"
echo ""
docker-compose -f docker-compose.local.yml ps

echo ""

# Step 5: Wait for services to be ready
echo -e "${BLUE}⏳ Waiting for services to be ready...${NC}"
sleep 5

# Check orchestrator
if curl -s http://localhost:8004/health > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Orchestrator is ready (port 8004)${NC}"
else
    echo -e "${YELLOW}⚠️  Orchestrator not ready yet (may take a few more seconds)${NC}"
fi

# Check RAG
if curl -s http://localhost:8003/health > /dev/null 2>&1; then
    echo -e "${GREEN}✅ RAG service is ready (port 8003)${NC}"
else
    echo -e "${YELLOW}⚠️  RAG service not ready yet (may take a few more seconds)${NC}"
fi

echo ""

# Step 6: Show access information
echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   Services Ready!                                       ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "🌐 ${GREEN}Main Application:${NC} http://localhost:8004"
echo -e "🧪 ${GREEN}Test Page:${NC}      http://localhost:8004/static/test-ultimate.html"
echo -e "📊 ${GREEN}RAG Service:${NC}    http://localhost:8003"
echo ""
echo -e "${YELLOW}📝 Next Steps:${NC}"
echo "   1. Open http://localhost:8004 in your browser"
echo "   2. Click the TARA orb (bottom-right)"
echo "   3. Check browser console for TaraSensor logs"
echo "   4. View logs: docker-compose -f docker-compose.local.yml logs -f"
echo ""
echo -e "${YELLOW}📖 Documentation:${NC}"
echo "   - LOCAL_DEPLOYMENT_GUIDE.md - Complete deployment guide"
echo "   - ULTIMATE_ARCHITECTURE.md - Architecture overview"
echo ""

# Optional: Open browser (macOS only)
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo -e "${BLUE}🌐 Opening browser...${NC}"
    open http://localhost:8004/static/test-ultimate.html
fi

echo ""
echo -e "${GREEN}🎉 Ready to test TARA Ultimate!${NC}"
