#!/bin/bash
# Quick script to copy files into container

echo "📦 Copying Ultimate TARA files to container..."

docker cp /Users/amar/demo.davinciai/orchestra_daytona.v2/static/tara_sensor.js orchestrator-local:/app/static/tara-sensor.js
docker cp /Users/amar/demo.davinciai/orchestra_daytona.v2/static/tara-widget-ultimate-integration.js orchestrator-local:/app/static/tara-widget-ultimate-integration.js  
docker cp /Users/amar/demo.davinciai/orchestra_daytona.v2/static/test-ultimate.html orchestrator-local:/app/static/test-ultimate.html

echo "✅ Files copied!"
echo ""
echo "📋 Verifying files in container:"
docker exec orchestrator-local ls -lh /app/static/tara-* /app/static/test-*
