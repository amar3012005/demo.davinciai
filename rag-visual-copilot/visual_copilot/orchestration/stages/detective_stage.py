import time
from typing import Any, Dict, Optional, Set


async def run_detective_stage(
    *,
    app: Any,
    semantic_detective: Any,
    mission_brain: Any,
    mission: Any,
    schema: Any,
    goal: str,
    query: str,
    session_id: str,
    nodes: list,
    excluded_ids: Set[str],
    effective_hive_hints: list,
    subgoal_mode: str,
    strategy_authoritative: bool,
    label_policy: Dict[str, Any],
    domain_name: str,
    is_zero_shot: bool,
    current_url: str,
    current_dom_signature: str,
    verified_advance_active: bool,
    start_time: float,
    hints: str,
    logger,
    type_tags: set,
    detective_min_score: float,
    detective_ambiguous_band: float,
    max_detective_retries_per_subgoal: int,
    tara_router_v2_enabled: bool,
    node_matches_expected_labels_fn,
    action_tag_compatible_fn,
    node_matches_strategy_focus_fn,
    candidate_signature_fn,
    canonicalize_label_fn,
    run_tier3_fallback_fn,
    record_and_maybe_advance_fn,
    extract_type_text_fn,
) -> Optional[Dict[str, Any]]:
    if label_policy.get("has_explicit_label"):
        logger.info(
            f"SEMANTIC_FALLBACK_AFTER_KEYWORD_MISS reason={label_policy.get('miss_reason') or 'unknown'}"
        )
    logger.info(f"🔍 Step 6: Semantic Detective investigating (excluding {len(excluded_ids)} IDs)...")

    _reject_all = getattr(app.state, "_detective_reject_signatures", {})
    if not hasattr(app.state, "_detective_reject_signatures"):
        app.state._detective_reject_signatures = _reject_all
    _reject_session = _reject_all.setdefault(session_id, {})

    try:
        detective_report = await semantic_detective.investigate(
            session_id=session_id,
            query=query,
            hive_hints=effective_hive_hints,
            action_intent=schema.action,
            excluded_ids=list(excluded_ids),
            subgoal_mode=subgoal_mode,
        )
    except TypeError as e:
        if "unexpected keyword argument 'subgoal_mode'" not in str(e):
            raise
        logger.warning(
            "⚠️ Detective compatibility fallback: investigate() has no "
            "subgoal_mode. Retrying with legacy signature."
        )
        detective_report = await semantic_detective.investigate(
            session_id=session_id,
            query=query,
            hive_hints=effective_hive_hints,
            action_intent=schema.action,
            excluded_ids=list(excluded_ids),
        )

    if (
        label_policy.get("has_explicit_label")
        and not label_policy.get("resolved_target_id")
        and detective_report.candidates
    ):
        node_by_id = {getattr(n, "id", ""): n for n in nodes}
        for cand in detective_report.candidates:
            node = node_by_id.get(getattr(cand, "node_id", ""))
            is_type_compatible = (
                subgoal_mode == "literal_type" and (cand.tag or "").lower() in type_tags
            )
            if is_type_compatible:
                continue
            if not node_matches_expected_labels_fn(node, label_policy["label_candidates"], domain_name):
                cand.hybrid_score = max(0.0, cand.hybrid_score * 0.2)
                if isinstance(getattr(cand, "score_breakdown", None), dict):
                    cand.score_breakdown["label_floor_penalty"] = 0.2
                    cand.score_breakdown["final"] = cand.hybrid_score
                if "label_floor_penalty" not in (cand.reasons or []):
                    cand.reasons.append("label_floor_penalty")

        detective_report.candidates.sort(
            key=lambda c: getattr(c, "hybrid_score", 0.0),
            reverse=True,
        )
        detective_report.best_match = detective_report.candidates[0] if detective_report.candidates else None

    strong_detector_accept = False
    if detective_report.best_match:
        _best = detective_report.best_match
        _score = _best.hybrid_score
        _zone_ok = (_best.zone or "").lower() in {"nav", "sidebar", "header", "main"}
        _compat = action_tag_compatible_fn(subgoal_mode, _best)
        if _score >= 0.70 and _compat and _zone_ok:
            strong_detector_accept = True
            logger.info(
                f"ROUTER_GUARD override=strong_detector_accept "
                f"score={_score:.2f} tag={_best.tag} zone={_best.zone}"
            )

    pre_rerank_best = detective_report.best_match
    pre_rerank_best_id = getattr(pre_rerank_best, "node_id", "") if pre_rerank_best else ""
    pre_rerank_best_score = (
        getattr(pre_rerank_best, "hybrid_score", 0.0)
        if pre_rerank_best
        else 0.0
    )
    pre_rerank_best_label_match = False
    if label_policy.get("has_explicit_label") and pre_rerank_best:
        _pre_node = next((n for n in nodes if getattr(n, "id", "") == pre_rerank_best_id), None)
        pre_rerank_best_label_match = node_matches_expected_labels_fn(
            _pre_node, label_policy["label_candidates"], domain_name
        )

    skip_llm_rerank = bool(
        strategy_authoritative
        and label_policy.get("has_explicit_label")
        and pre_rerank_best
        and pre_rerank_best_label_match
        and pre_rerank_best_score >= 0.90
        and action_tag_compatible_fn(subgoal_mode, pre_rerank_best)
    )
    if skip_llm_rerank:
        logger.info(
            f"ROUTER_GUARD rerank_bypass=strategy_label_lock score={pre_rerank_best_score:.2f} "
            f"candidate='{getattr(pre_rerank_best, 'text', '')[:80]}'"
        )

    if (
        (not strong_detector_accept)
        and (not skip_llm_rerank)
        and detective_report.candidates
        and len(detective_report.candidates) > 1
        and hasattr(app.state, "mind_reader")
        and hasattr(app.state.mind_reader, "llm")
    ):
        top_n = detective_report.candidates[:10]
        logger.info(f"🧠 LLM deciding the absolute Best Match from top {len(top_n)} candidates...")

        previous_subgoal = ""
        if mission.current_subgoal_index > 0 and mission.current_subgoal_index - 1 < len(mission.subgoals):
            previous_subgoal = mission.subgoals[mission.current_subgoal_index - 1]

        candidates_text = "\n".join(
            [
                f"[{i}]: text='{c.text[:50]}' tag='{c.tag}' zone='{c.zone}' "
                f"score={c.hybrid_score:.2f}"
                for i, c in enumerate(top_n)
            ]
        )

        prompt = (
            "You are a strict UI action analyst.\n"
            "Choose the single best candidate for the current subgoal using mission context.\n"
            f"User Goal: {goal}\n"
            f"Main Goal (schema): {schema.target_entity}\n"
            f"Previous Subgoal: {previous_subgoal or 'none'}\n"
            f"Current Subgoal: {query}\n"
            f"Action Mode: {subgoal_mode}\n"
            f"Strategy Locked: {strategy_authoritative}\n"
            f"Explicit Labels: {label_policy.get('label_candidates', [])}\n"
            f"Top {len(top_n)} Candidates:\n{candidates_text}\n\n"
            "Rules:\n"
            "1) Prefer exact/near-exact text match to Current Subgoal labels.\n"
            "2) If strategy is locked, do not drift from current subgoal intent.\n"
            "3) For typing/search tasks choose only input/textarea/select.\n"
            "4) For click tasks choose actionable/clickable targets.\n"
            "5) If no candidate fits, return -1.\n\n"
            f"Return ONLY integer index [0-{len(top_n)-1}] or -1.\nNo explanation."
        )

        try:
            llm_response = await app.state.mind_reader.llm.generate(
                prompt,
                model="llama-3.1-8b-instant",
                temperature=0.0,
            )
            import re

            match = re.search(r"-?\d+", llm_response)
            if match:
                chosen_idx = int(match.group())
                if chosen_idx == -1:
                    logger.warning("   🛑 LLM Reranker REJECTED all candidates. Forcing V1 Fallback.")
                    if detective_report.best_match:
                        detective_report.best_match.hybrid_score = 0.1
                        detective_report.best_match.hybrid_score = 0.1
                        detective_report.confidence = "low"
                elif 0 <= chosen_idx < len(top_n):
                    chosen_candidate = top_n[chosen_idx]
                    is_typing_action = "type" in query.lower() or "search" in query.lower()
                    is_input_tag = chosen_candidate.tag.lower() in ["input", "textarea", "select"]
                    chosen_node = next(
                        (n for n in nodes if getattr(n, "id", "") == getattr(chosen_candidate, "node_id", "")),
                        None,
                    )
                    label_safe = True
                    if label_policy.get("has_explicit_label"):
                        label_safe = node_matches_expected_labels_fn(
                            chosen_node, label_policy["label_candidates"], domain_name
                        )

                    if is_typing_action and not is_input_tag:
                        logger.warning(
                            f"   🛑 TAG MISMATCH: Subgoal wants to TYPE, but candidate is '{chosen_candidate.tag}'. Tanking score to force V1 Fallback."
                        )
                        detective_report.best_match = chosen_candidate
                        detective_report.best_match.hybrid_score = 0.1
                        detective_report.best_match.hybrid_score = 0.1
                        detective_report.confidence = "low"
                        _sig = candidate_signature_fn(
                            chosen_candidate.text, chosen_candidate.tag, chosen_candidate.zone
                        )
                        _reject_session[_sig] = _reject_session.get(_sig, 0) + 1
                    elif subgoal_mode == "literal_click" and not action_tag_compatible_fn(subgoal_mode, chosen_candidate):
                        logger.warning(
                            f"   🛑 TAG MISMATCH: Subgoal wants CLICK, but candidate is '{chosen_candidate.tag}'. "
                            "Keeping detector-selected actionable candidate."
                        )
                    elif label_policy.get("has_explicit_label") and not label_safe:
                        logger.warning(
                            "ROUTER_GUARD rerank_reject=label_mismatch "
                            f"candidate='{chosen_candidate.text[:80]}' labels={label_policy['label_candidates'][:2]}"
                        )
                        if pre_rerank_best:
                            detective_report.best_match = pre_rerank_best
                            detective_report.confidence = (
                                "medium" if pre_rerank_best_score >= detective_min_score else detective_report.confidence
                            )
                            logger.info(
                                f"ROUTER_GUARD rerank_revert candidate='{getattr(pre_rerank_best, 'text', '')[:80]}' "
                                f"score={pre_rerank_best_score:.2f}"
                            )
                    else:
                        logger.info(
                            f"   🤖 LLM ranked candidate {chosen_idx} as best: '{chosen_candidate.text}' (was original rank #{chosen_idx+1})"
                        )
                        detective_report.best_match = chosen_candidate
                        if detective_report.best_match.hybrid_score < 0.35:
                            detective_report.confidence = "low"
                else:
                    logger.warning(f"   ⚠️ LLM returned out of bounds index: {chosen_idx}")
            else:
                logger.warning(f"   ⚠️ LLM returned non-integer: '{llm_response}'")
        except Exception as e:
            logger.error(f"   ⚠️ LLM reranking failed: {e}")

    if label_policy.get("has_explicit_label") and detective_report.best_match:
        _best_node = next(
            (n for n in nodes if getattr(n, "id", "") == getattr(detective_report.best_match, "node_id", "")),
            None,
        )
        if not node_matches_expected_labels_fn(_best_node, label_policy["label_candidates"], domain_name):
            logger.warning(
                "SEMANTIC_FALLBACK_AFTER_KEYWORD_MISS reason=best_candidate_label_mismatch "
                f"labels={label_policy['label_candidates'][:2]}"
            )
            if pre_rerank_best and pre_rerank_best_label_match:
                detective_report.best_match = pre_rerank_best
                logger.info(
                    f"ROUTER_GUARD rerank_revert reason=label_mismatch candidate='{getattr(pre_rerank_best, 'text', '')[:80]}' "
                    f"score={pre_rerank_best_score:.2f}"
                )
            else:
                detective_report.best_match.hybrid_score = 0.1
                detective_report.best_match.hybrid_score = 0.1
                detective_report.confidence = "low"

    _compat_ok = action_tag_compatible_fn(subgoal_mode, detective_report.best_match) if detective_report.best_match else False
    if detective_report.best_match and not _compat_ok:
        logger.warning(
            f"   🛑 ROUTER_GUARD: candidate incompatible with mode={subgoal_mode} "
            f"(tag={detective_report.best_match.tag}). Forcing Tier 3."
        )
        _sig = candidate_signature_fn(
            detective_report.best_match.text,
            detective_report.best_match.tag,
            detective_report.best_match.zone,
        )
        _reject_session[_sig] = _reject_session.get(_sig, 0) + 1
        detective_report.best_match.hybrid_score = 0.1
        detective_report.best_match.hybrid_score = 0.1
        detective_report.confidence = "low"

    if detective_report.best_match and strategy_authoritative and subgoal_mode == "literal_click":
        _strategy_node = next(
            (n for n in nodes if getattr(n, "id", "") == getattr(detective_report.best_match, "node_id", "")),
            None,
        )
        if _strategy_node and not node_matches_strategy_focus_fn(_strategy_node, query):
            logger.warning(
                "ROUTER_GUARD strategy_locked_mismatch=true "
                f"query='{query[:80]}' candidate='{getattr(detective_report.best_match, 'text', '')[:80]}'"
            )
            detective_report.best_match.hybrid_score = 0.1
            detective_report.best_match.hybrid_score = 0.1
            detective_report.confidence = "low"
        elif _strategy_node:
            _score = detective_report.best_match.hybrid_score
            if _score >= detective_min_score and action_tag_compatible_fn(subgoal_mode, detective_report.best_match):
                if detective_report.confidence == "low":
                    logger.info(
                        "ROUTER_GUARD override=strategy_focus_match "
                        f"query='{query[:80]}' candidate='{getattr(detective_report.best_match, 'text', '')[:80]}' "
                        f"score={_score:.2f}"
                    )
                detective_report.confidence = "medium"

    if detective_report.best_match and tara_router_v2_enabled:
        _sig = candidate_signature_fn(
            detective_report.best_match.text,
            detective_report.best_match.tag,
            detective_report.best_match.zone,
        )
        if _reject_session.get(_sig, 0) >= max_detective_retries_per_subgoal:
            logger.warning(
                f"   🛑 LOOP_GUARD: repeated rejected signature '{_sig[:72]}' "
                f"count={_reject_session.get(_sig, 0)}. Forcing Tier 3."
            )
            detective_report.best_match.hybrid_score = 0.1
            detective_report.best_match.hybrid_score = 0.1
            detective_report.confidence = "low"

    logger.info(
        f"   ✅ Final Best Match: {detective_report.best_match.text if detective_report.best_match else 'none'} "
        f"(score: {detective_report.best_match.hybrid_score if detective_report.best_match else 0:.2f})"
    )

    explicit_label_lock = False
    if detective_report.candidates and len(detective_report.candidates) > 1 and detective_report.best_match:
        _best_id = getattr(detective_report.best_match, "node_id", "")
        _best_text_can = canonicalize_label_fn(getattr(detective_report.best_match, "text", "") or "")
        _next_other = next(
            (
                c
                for c in detective_report.candidates
                if getattr(c, "node_id", "") != _best_id
                and canonicalize_label_fn(getattr(c, "text", "") or "") != _best_text_can
            ),
            None,
        )
        if _next_other is None:
            _delta = 1.0
        else:
            _delta = (
                detective_report.best_match.hybrid_score
                - _next_other.hybrid_score
            )
        if (not strong_detector_accept) and _delta < detective_ambiguous_band:
            detective_report.confidence = "low"
            logger.warning(
                f"   ⚠️ ROUTER_GUARD: Ambiguous detector ranking delta={_delta:.2f} "
                f"< band={detective_ambiguous_band:.2f}. Forcing low confidence."
            )

    if label_policy.get("has_explicit_label") and detective_report.best_match:
        _best_node = next(
            (n for n in nodes if getattr(n, "id", "") == getattr(detective_report.best_match, "node_id", "")),
            None,
        )
        _best_score = detective_report.best_match.hybrid_score
        if (
            _best_node
            and node_matches_expected_labels_fn(_best_node, label_policy["label_candidates"], domain_name)
            and _best_score >= max(0.50, detective_min_score)
            and action_tag_compatible_fn(subgoal_mode, detective_report.best_match)
        ):
            if detective_report.confidence == "low":
                logger.info(
                    f"ROUTER_GUARD override=explicit_label_match score={_best_score:.2f} "
                    f"labels={label_policy['label_candidates'][:2]}"
                )
            detective_report.confidence = "medium"
            explicit_label_lock = True

    if detective_report.best_match:
        best_score = detective_report.best_match.hybrid_score
        needs_tier3 = (
            (not explicit_label_lock and detective_report.confidence == "low")
            or best_score < detective_min_score
        )
        if strong_detector_accept and best_score >= detective_min_score:
            needs_tier3 = False
            logger.info("ROUTER_GUARD bypass=tier3 strong_detector_accept=true")

        if needs_tier3:
            _sig = candidate_signature_fn(
                detective_report.best_match.text,
                detective_report.best_match.tag,
                detective_report.best_match.zone,
            )
            _reject_session[_sig] = _reject_session.get(_sig, 0) + 1
            tier3_result = await run_tier3_fallback_fn(
                app=app,
                goal=goal,
                query=query,
                nodes=nodes,
                mission=mission,
                mission_brain=mission_brain,
                schema=schema,
                is_zero_shot=is_zero_shot,
                start_time=start_time,
                forced_reason=f"detective_score={best_score:.2f}",
                excluded_ids=excluded_ids,
                expected_labels=label_policy["label_candidates"] if label_policy.get("has_explicit_label") else None,
                expected_domain=domain_name,
                current_url=current_url,
                dom_signature=current_dom_signature,
                verified_advance_active=verified_advance_active,
                strategy_authoritative=strategy_authoritative,
            )
            if tier3_result:
                tier3_result["router_mode"] = subgoal_mode
                tier3_result["detective_used"] = True
                tier3_result["detective_score"] = best_score
                tier3_result["fallback_tier"] = "tier3_after_detective"
                return tier3_result

            logger.warning("   🚫 All 3 tiers failed. Blocking action.")

            # Record the failure in mission history so the amnesia guard
            # can detect it on the next request and invalidate this mission,
            # allowing a fresh pre-decision strategy to be used.
            try:
                if mission and mission_brain:
                    mission.action_history.append({
                        "result": "all_tiers_failed",
                        "query": query,
                        "subgoal_index": getattr(mission, "current_subgoal_index", -1),
                    })
                    await mission_brain._save_mission(mission)
            except Exception as _save_err:
                logger.warning(f"   ⚠️ Could not persist failure marker: {_save_err}")

            return {
                "success": False,
                "blocked": True,
                "reason": f"I cannot confidently find '{query}' on this screen.",
                "action": None,
                "mission_id": mission.mission_id,
                "no_legacy_fallback": bool(getattr(mission, "strategy_locked", False)),
                "router_mode": subgoal_mode,
                "detective_used": True,
                "detective_score": best_score,
                "fallback_tier": "tier3_failed",
            }

        logger.info("🛡️ Step 7: Mission Brain auditing action...")
        approved, reason = await mission_brain.audit_action(
            mission_id=mission.mission_id,
            action_type=detective_report.recommended_action,
            target_id=detective_report.best_match.node_id,
            target_text=detective_report.best_match.text,
            detective_report=detective_report,
        )

        if not approved:
            logger.warning(f"   🚫 Action BLOCKED: {reason}")
            await mission_brain.advance_subgoal(
                mission.mission_id,
                is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
            )
            return {
                "success": False,
                "blocked": True,
                "reason": reason,
                "action": None,
            }

        logger.info(f"   ✅ Action APPROVED: {detective_report.recommended_action}")
        action_type = detective_report.recommended_action
        node_id = detective_report.best_match.node_id if detective_report.best_match else ""
        text_to_type = ""
        pending_action_type = "click"
        if action_type in {"type", "type_text"}:
            pending_action_type = "type_text"
            text_to_type = extract_type_text_fn(query, schema.target_entity)

        await record_and_maybe_advance_fn(
            mission_brain=mission_brain,
            mission=mission,
            action_type=pending_action_type,
            target_id=node_id,
            nodes=nodes,
            current_url=current_url,
            dom_signature=current_dom_signature,
            text=text_to_type,
            verified_advance_active=verified_advance_active,
            is_zero_shot=getattr(schema, "zero_shot_mode", False) or is_zero_shot,
        )

    elapsed_ms = int((time.time() - start_time) * 1000)
    _det_label = (
        detective_report.best_match.text[:40].strip()
        if detective_report.best_match and detective_report.best_match.text
        else "the element"
    )
    _det_action = detective_report.recommended_action
    _action_wire = "type_text" if _det_action in {"type", "type_text"} else "click"
    _det_speech = f"{'Clicking on' if _action_wire == 'click' else 'Typing into'} {_det_label}..."

    return {
        "success": True,
        "blocked": False,
        "action": {
            "type": _action_wire,
            "target_id": detective_report.best_match.node_id if detective_report.best_match else "",
            "text": detective_report.best_match.text if detective_report.best_match else "",
        },
        "speech": _det_speech,
        "mission_id": mission.mission_id,
        "subgoal_index": mission.current_subgoal_index,
        "confidence": detective_report.confidence,
        "timing_ms": elapsed_ms,
        "pipeline": "ultimate_tara",
        "gps_hints": hints,
        "router_mode": subgoal_mode,
        "detective_used": True,
        "detective_score": detective_report.best_match.hybrid_score
        if detective_report.best_match
        else 0.0,
        "fallback_tier": "detective",
        "pending_verification": verified_advance_active,
    }
