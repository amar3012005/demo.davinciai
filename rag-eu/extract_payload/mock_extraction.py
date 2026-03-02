import json

data = [
  {
    "doc_type": "Website_Map",
    "domain": "console.groq.com",
    "url": "https://console.groq.com/",
    "concept": "Navigate to and evaluate GroqCloud Dashboard",
    "sequence": ["Locate Navigation", "Login to Console", "Access API Keys", "Enter Playground", "View Documentation"],
    "blocking_rules": ["Do not access API keys before logging in", "Do not enter playground until workspace is verified"],
    "action_script": "await page.goto('https://console.groq.com/'); await page.click('text=Log In'); await page.click('text=API Keys');"
  },
  {"doc_type": "Visual_Hint", "selector": "a[href*='/login']", "keyword_match": "/login", "element_type": "a", "text_pattern": "Log In", "zone": "nav", "description": "Navigation link to log into console"},
  {"doc_type": "Visual_Hint", "selector": "a[href*='/keys']", "keyword_match": "/keys", "element_type": "a", "text_pattern": "API Keys", "zone": "nav", "description": "Navigation link to manage API keys"},
  {"doc_type": "Visual_Hint", "selector": "a[href*='/playground']", "keyword_match": "/playground", "element_type": "a", "text_pattern": "Playground", "zone": "nav", "description": "navigation link to prompt playground"},
  {"doc_type": "Visual_Hint", "selector": "a[href*='/docs/overview']", "keyword_match": "/docs/overview", "element_type": "a", "text_pattern": "Docs", "zone": "nav", "description": "Link to documentation overview"},

  {
    "doc_type": "Website_Map",
    "domain": "console.groq.com",
    "url": "https://console.groq.com/docs/quickstart",
    "concept": "Navigate to and evaluate Groq API generic setup",
    "sequence": ["Locate Documentation Guide", "Verify Prerequisites", "Install SDK", "Set up API Key", "Run sample request"],
    "blocking_rules": ["Do not run sample request until SDK is installed", "Do not set up API key before generating one"],
    "action_script": "await page.goto('https://console.groq.com/docs/quickstart'); await page.click('text=Set up your API Key');"
  },
  {"doc_type": "Visual_Hint", "selector": "a[href*='/docs/quickstart']", "keyword_match": "/docs/quickstart", "element_type": "a", "text_pattern": "Quickstart", "zone": "nav", "description": "Navigation link generic quickstart"},
  {"doc_type": "Visual_Hint", "selector": "a[href*='/docs/models']", "keyword_match": "/docs/models", "element_type": "a", "text_pattern": "Models", "zone": "nav", "description": "Navigation link model list"},
  {"doc_type": "Visual_Hint", "selector": "a[href*='/docs/api-reference']", "keyword_match": "/docs/api-reference", "element_type": "a", "text_pattern": "API Reference", "zone": "nav", "description": "Navigation link to API REST spec"},
  
  {
    "doc_type": "Website_Map",
    "domain": "console.groq.com",
    "url": "https://console.groq.com/docs/models",
    "concept": "Learn about deployed LPU inference models",
    "sequence": ["Access Models section", "Verify Llama 3 context limits", "Check Whisper audio limits"],
    "blocking_rules": ["Do not attempt payload without verifying context limit"],
    "action_script": "await page.goto('https://console.groq.com/docs/models');"
  },
  {"doc_type": "Visual_Hint", "selector": "a[href*='/openai']", "keyword_match": "OpenAI", "element_type": "a", "text_pattern": "OpenAI Compatibility", "zone": "main", "description": "Link to OpenAI compatibility guide"},
  {"doc_type": "Visual_Hint", "selector": "a[href*='/responses-api']", "keyword_match": "Responses", "element_type": "a", "text_pattern": "Responses API", "zone": "main", "description": "Link to responses API page"},
  
  {
    "doc_type": "Website_Map",
    "domain": "console.groq.com",
    "url": "https://console.groq.com/docs/api-reference",
    "concept": "Verify Groq HTTP JSON Endpoints",
    "sequence": ["Access API Reference", "View Chat Completions schema", "Check Transcriptions endpoint setup"],
    "blocking_rules": ["Do not construct REST queries before verifying endpoints"],
    "action_script": "await page.goto('https://console.groq.com/docs/api-reference');"
  },
  {"doc_type": "Visual_Hint", "selector": "a[href*='#chat-create']", "keyword_match": "chat-create", "element_type": "a", "text_pattern": "Create chat completion", "zone": "main", "description": "Link to POST chat completion"},
  {"doc_type": "Visual_Hint", "selector": "a[href*='#audio-transcription']", "keyword_match": "audio-transcription", "element_type": "a", "text_pattern": "Create transcription", "zone": "main", "description": "Link to POST audio transcription"},
  {"doc_type": "Visual_Hint", "selector": "a[href*='#batches-create']", "keyword_match": "batches-create", "element_type": "a", "text_pattern": "Create batch", "zone": "main", "description": "Link to POST batch"}
]

with open("/Users/amar/demo.davinciai/rag-daytona.v2/extract_payload/console_groq_extracted_hivemind.json", "w") as f:
    json.dump(data, f, indent=2)

print("Saved exhaustive set!")
