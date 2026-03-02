# Visual CoPilot Prompts

This folder is the source of truth for Visual CoPilot prompt assets.

## Structure
- `mission_end/`: mission completion validation and end-dialogue prompts.
- `react/`: ReAct navigator prompts.
- `mind_reader/`: domain-specific intent prompts.

## Rules
1. Keep prompt outputs schema-constrained.
2. Include `json` wording when calling `response_format={"type": "json_object"}`.
3. Do not embed runtime business logic in prompt files.
4. Keep compatibility wrappers in `prompts/` and `mind_reader_domain_prompts/` paths.
