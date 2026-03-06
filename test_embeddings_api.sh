#!/bin/bash
curl -X POST http://localhost:4016/embed \
  -H "Content-Type: application/json" \
  -d '{"sentences": ["Hello, this is a test.", "This is another test sentence."]}'
