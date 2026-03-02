# 03 Add New Domain Prompt

## Preconditions
- Domain behavior requirements documented.

## Steps
1. Add `visual_copilot/prompts/mind_reader/<domain>.py`.
2. Add compatibility wrapper in `mind_reader_domain_prompts/<domain>.py`.
3. Keep output schema consistent with mind-reader parser.
4. Add scenario tests for extraction/search/navigation on that domain.

## Failure Symptoms and Fixes
- JSON parse error: ensure strict JSON output.
- Wrong domain routing: verify module naming normalization.

## Verification Checklist
- `Mind Reader: Using domain-specific prompt` log appears.
