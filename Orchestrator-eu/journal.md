# Orchestrator-EU — Development Journal

---

## 2026-03-20

### Session: bundb branch — EU Strategic-Agent Upgrade

#### Fixes applied

**pipeline.py**
- Fixed token assembly whitespace collapse in completion log. `"".join(full_answer)` replaced with boundary-checked assembly — inserts space between tokens when neither side has whitespace. Prevents "Thatsoundslike" in logs and browser text.

**service_client.py (`TTSClient`)**
- Added `reset_buffer()` method — clears `_chunk_buffer` and cancels `_flush_task` without closing the WebSocket.
- Eliminates TTS reconnect on every batch turn. Previously `abort_stream()` closed the socket before each synthesis, forcing a full reconnect (~1946ms → ~800ms TTFC expected).

**ws_handler.py**
- Main batch path: replaced `abort_stream()` with `reset_buffer()` before synthesis.
- Visual-copilot batch path: same replacement.
- Default `policy_mode="sales"` always injected into `_context_data` before pipeline call.
- `previous_context` passed to `evaluate_turn` for stateful multi-turn policy.
- Remaining `abort_stream()` calls preserved for legitimate cases: zero-audio failure, playback watchdog timeout, barge-in interrupt.

**rag_engine.py**
- `policy_snapshot` always built with `policy_mode_default` fallback — ensures compact sales prompt instead of full 4000-char Zone A+B.
- Empty token guard in streaming callback: `if streaming_callback and text:` (was `if streaming_callback:`).
- Qdrant timeout reduced 2.5s → 1.0s for non-dashboard queries.
- Groq translation skipped for `sales` and `clinical` modes (~200ms saving — multilingual embeddings handle German natively).
- `tts_safe()` removed from per-token streaming path (was corrupting partial tokens).

**app.py**
- `tts_safe()` removed from individual streaming tokens in `callback()`. Still applied to full assembled answer in cache-hit path.

**context_architecture_bundb.py**
- `_render_compact_sales_prefix` — added `## Aktives Zuhören` section with reflect→interpret→ask pattern.
- Zone C history depth reduced: `history[-4:]` → `history[-3:]`.
- Zone D deduplication: removed `tenant_memory` and `knowledge_base` blocks (~3200 chars saved per prompt).

**config.py**
- `policy_mode_default = "sales"` confirmed.

---

#### Feature: symptoX-style structured reasoning (brand consulting)

Implemented a hypothetico-deductive reasoning loop in the `clinical` policy mode. "Clinical" here means **structured brand consulting intake** — not healthcare. Same methodology: generate hypotheses, rank by urgency, ask the one most discriminating question, reflect before asking.

**conversation_policy.py**

New `DIFFERENTIAL_MAP` — 7 brand problem clusters, each with 3–4 competing root-cause hypotheses ranked by business impact (danger 1–10):
- `brand_positioning`: unclear_USP(9), wrong_target_audience(8), me_too_positioning(7), premature_narrowing(3)
- `brand_messaging`: inconsistent_voice(8), too_complex(7), too_generic(6), wrong_register(4)
- `brand_identity`: no_coherent_identity(8), misaligned_with_positioning(7), outdated(5), over_engineered(3)
- `brand_awareness`: wrong_channels(8), inconsistent_presence(7), insufficient_reach(6), no_earned_media(5)
- `brand_trust`: no_proof_of_expertise(9), negative_reputation(8), authority_mismatch(6), faceless_brand(4)
- `growth_strategy`: brand_funnel_disconnected(8), unclear_value_prop(8), wrong_segment(7), retention(6)
- `competitive_pressure`: undifferentiated(9), disruptive_entrant(8), being_copied(7), commodity_trap(6)

New `SYMPTOM_TO_DX_KEY` — maps brand problem keywords (DE + EN) to cluster keys.

New `_score_differentials()` — scores each candidate hypothesis against reported signals and negative cues. Penalises (-4 danger) when a negative cue matches a ruling-out feature.

Updated `_choose_next_question_focus()` — now differential-driven:
- danger ≥ 8 → `dx_confirm:DX:feature` (confirm the urgent hypothesis)
- two viable candidates → `dx_discriminate:AvB:feature` (ask what splits them)
- single candidate → `dx_probe:DX:feature`
- fallback → slot-based (brand_trigger / timeline / urgency_level)

New fields on `ConversationPolicyDecision`: `ranked_differentials`, `confirmed_dx`, `ruled_out_dx` — all passed through `as_context()`.

`CLINICAL_RED_FLAGS` → urgent business moments: investor pitch, launch deadline, funding round, brand crisis.
`CLINICAL_SYMPTOMS` → brand problem signals: positioning, messaging, identity, awareness, trust, growth, competition.
Slots renamed to brand context: `brand_trigger`, `timeline`, `urgency_level`, `context_trigger`, `existing_assets`.

**context_architecture_bundb.py**

`_render_compact_clinical_prefix` — rewritten with 3-step internal CoT:
1. Rank hypotheses by urgency using `ranked_differentials` in `<policy>`
2. Identify most discriminating missing fact using `next_question_focus`
3. Format: reflection (1 sentence) + one question (1 sentence) — no reasoning in output

Policy block in `_render_zone_c` now includes `ranked_differentials=unclear_USP(danger=9) | me_too_positioning(danger=7) | ...` for clinical mode.

`behavior_block` for clinical mode updated — Tara is "strategische Beraterin", clarifies before recommending.

**rag_engine.py**

`_build_clinical_follow_up_question` — 50+ brand strategy questions organised by `{dx}:{feature}` keys. Covers all major hypotheses. Parses `dx_confirm:`, `dx_discriminate:`, `dx_probe:` key format from the policy layer.

`_compose_policy_answer` — reflection injection: if clinical+probe and response is a bare question, prepends "Das klingt nach einem Thema rund um {problem_area}." to enforce the reflect-then-ask format.

---

#### Known pending items

- STT callback silent transcript loss on 2s timeout — not yet fixed
- TTS `context_id` queue leak on connection failure — not yet fixed

---

## 2026-03-20 (continued)

### Fix: context_architecture_davinci.py — signature mismatch with rag_engine

**Problem:** `rag_engine.py` calls `assemble_prompt` with 4 extra kwargs that the davinci architecture didn't accept:
- `interrupted_text`, `interruption_transcripts`, `interruption_type`, `user_id`
This caused a `TypeError` at runtime for any davinci-tenant request.

Additionally, `_render_zone_c` didn't use `hive_mind` at all — so policy blocks, hivemind KB, and interruption context were silently dropped for davinci tenants.

**Fix (`context_architecture/context_architecture_davinci.py`):**
- `assemble_prompt` — added 4 missing optional params, forwarded to `_render_zone_c`
- `_render_zone_c` — updated signature; added `hivemind_block` (`<hm>`), `policy_block` (`<policy>`), and `interruption_block` matching bundb pattern
- `_render_zone_d` — added `hive_mind=None` kwarg for API compatibility
- All davinci-specific content (AIDA framework, sales rules, examples) left unchanged

**Signature now matches rag_engine call exactly.**

---
