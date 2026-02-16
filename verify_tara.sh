#!/bin/bash
# Verify TARA v4 Plan Next Step Endpoint

echo "🧪 Running TARA v4 Verification Test..."

curl -k -X POST https://localhost:8444/api/v1/plan_next_step \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "test-session-'$(date +%s)'",
    "goal": "Go to the pricing page and find the enterprise plan",
    "dom_context": [
      {"type": "nav", "id": "main-nav", "text": "Home Pricing About", "interactive": true},
      {"type": "a", "id": "link-pricing", "text": "Pricing", "interactive": true},
      {"type": "h1", "text": "Welcome to Daytona", "id": "hero-title"},
      {"type": "button", "text": "Sign Up", "id": "btn-signup", "interactive": true}
    ],
    "step_number": 0,
    "current_url": "https://daytona.io",
    "client_id": "test-client"
  }' | json_pp

echo -e "\n\nDone."
