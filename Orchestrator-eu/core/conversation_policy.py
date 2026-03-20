"""
Lightweight strategic policy layer for the EU orchestrator.

This module keeps turn policy out of the prompt and off the transport path.
It is intentionally heuristic-first to preserve latency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config_loader import PolicyConfig
from core.history_manager import Turn


@dataclass
class StructuredObservation:
    tenant_id: str
    session_id: str
    turn_id: str
    doc_type: str
    topic: str
    confidence: float
    issue: str
    solution: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationPolicyDecision:
    enabled: bool
    policy_mode: str = "sales"
    conversation_stage: str = "general"
    response_act: str = "answer"
    hypotheses: List[str] = field(default_factory=list)
    missing_slots: List[str] = field(default_factory=list)
    retrieval_profile: str = "default"
    memory_write_candidates: List[StructuredObservation] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    ranked_differentials: List[Dict[str, Any]] = field(default_factory=list)
    confirmed_dx: List[str] = field(default_factory=list)
    ruled_out_dx: List[str] = field(default_factory=list)

    def as_context(self) -> Dict[str, Any]:
        return {
            "policy_mode": self.policy_mode,
            "conversation_stage": self.conversation_stage,
            "response_act": self.response_act,
            "hypotheses": self.hypotheses,
            "missing_slots": self.missing_slots,
            "retrieval_profile": self.retrieval_profile,
            "policy_metadata": self.metadata,
            "ranked_differentials": [
                {"dx": d["dx"], "category": d.get("category", ""), "danger": d["danger"]}
                for d in self.ranked_differentials[:3]
            ],
            "confirmed_dx": self.confirmed_dx,
            "ruled_out_dx": self.ruled_out_dx,
        }


class ConversationPolicyManager:
    """Fast heuristic stage classifier and structured observation extractor."""

    SALES_CLOSE_KEYWORDS = (
        "price", "pricing", "cost", "budget", "buy", "demo", "meeting", "call me",
        "callback", "contact", "email", "phone", "angebot", "preis", "termin",
    )
    SALES_OBJECTION_KEYWORDS = (
        "expensive", "costly", "not sure", "unclear", "later", "already use", "concern",
        "teuer", "später", "unsicher", "bedenken", "zu kompliziert",
    )
    # Brand urgency red flags — situations where immediate strategic attention is needed
    CLINICAL_RED_FLAGS = (
        "launch next week", "launch morgen", "investor pitch", "investoren präsentation",
        "funding round", "finanzierungsrunde", "rebrand now", "sofort rebranden",
        "brand crisis", "markenkrise", "no brand at all", "deadline", "frist",
        "we have nothing", "wir haben nichts", "pitch next week", "präsentation nächste woche",
        "launch in a week", "going live soon", "launch soon",
    )
    # Brand problem signals — what prospects describe when they have a brand problem
    CLINICAL_SYMPTOMS = (
        "positioning", "positionierung", "differentiation", "differenzierung",
        "messaging", "botschaft", "communication", "kommunikation", "copy", "texte",
        "identity", "identität", "logo", "design", "visual", "ci",
        "awareness", "bekanntheit", "sichtbarkeit", "visibility", "recognition",
        "trust", "vertrauen", "credibility", "glaubwürdigkeit", "reputation",
        "growth", "wachstum", "leads", "conversion", "umsatz", "revenue",
        "competition", "wettbewerb", "competitor", "mitbewerber",
        "brand voice", "tonalität", "tone", "sprache",
    )
    # Signals that rule out certain brand problem hypotheses
    CLINICAL_NEGATIVE_CUES = (
        "we have brand guidelines", "brand guide exists", "wir haben ein markenbuch",
        "we already did research", "research done", "we have an agency",
        "already rebranded", "just rebranded", "strong reviews", "strong portfolio",
        "no budget", "kein budget", "not interested", "kein interesse",
    )
    # Timeline patterns — urgency signals in brand engagements
    CLINICAL_DURATION_PATTERNS = (
        (r"\b(\d+)\s*(week|weeks|woche|wochen)\b", "timeline_weeks"),
        (r"\b(\d+)\s*(month|months|monat|monate)\b", "timeline_months"),
        (r"\b(next|nächste[nrs]?)\s*(week|month|quarter|woche|monat|quartal)\b", "timeline_next_period"),
        (r"\b(asap|sofort|immediately|dringend)\b", "timeline_urgent"),
    )
    # Urgency / impact terms
    CLINICAL_SEVERITY_TERMS = (
        "urgent", "dringend", "critical", "kritisch", "high priority", "hohe priorität",
        "not urgent", "no rush", "whenever", "irgendwann", "low priority",
    )
    # Context triggers — what is causing the need for brand work
    CLINICAL_TRIGGER_TERMS = (
        "rebranding", "rebrand", "new product", "neues produkt", "pivot", "funding",
        "finanzierung", "merger", "fusion", "new market", "neuer markt",
        "new competition", "neue konkurrenz", "growth", "scaling", "skalierung",
    )

    # Maps brand problem keyword → cluster key in DIFFERENTIAL_MAP
    SYMPTOM_TO_DX_KEY: Dict[str, str] = {
        # Positioning signals
        "positioning": "brand_positioning", "positionierung": "brand_positioning",
        "differentiation": "brand_positioning", "differenzierung": "brand_positioning",
        "niche": "brand_positioning", "nische": "brand_positioning",
        "unique": "brand_positioning", "anders": "brand_positioning",
        # Messaging signals
        "messaging": "brand_messaging", "botschaft": "brand_messaging",
        "communication": "brand_messaging", "kommunikation": "brand_messaging",
        "copy": "brand_messaging", "texte": "brand_messaging",
        "brand voice": "brand_messaging", "tonalität": "brand_messaging",
        "tone": "brand_messaging", "sprache": "brand_messaging",
        # Identity signals
        "identity": "brand_identity", "identität": "brand_identity",
        "logo": "brand_identity", "design": "brand_identity",
        "visual": "brand_identity", "ci": "brand_identity",
        # Awareness signals
        "awareness": "brand_awareness", "bekanntheit": "brand_awareness",
        "sichtbarkeit": "brand_awareness", "visibility": "brand_awareness",
        "recognition": "brand_awareness",
        # Trust / credibility signals
        "trust": "brand_trust", "vertrauen": "brand_trust",
        "credibility": "brand_trust", "glaubwürdigkeit": "brand_trust",
        "reputation": "brand_trust",
        # Growth signals
        "growth": "growth_strategy", "wachstum": "growth_strategy",
        "leads": "growth_strategy", "conversion": "growth_strategy",
        "umsatz": "growth_strategy", "revenue": "growth_strategy",
        # Competitive pressure
        "competition": "competitive_pressure", "wettbewerb": "competitive_pressure",
        "competitor": "competitive_pressure", "mitbewerber": "competitive_pressure",
    }

    # Brand differential map — brand problem clusters with competing root-cause hypotheses.
    # danger: 1=low_impact to 10=business_critical
    # confirm: signals that make this hypothesis more likely
    # rule_out: signals that make this hypothesis less likely
    DIFFERENTIAL_MAP: Dict[str, List[Dict[str, Any]]] = {
        "brand_positioning": [
            {"dx": "unclear_USP", "category": "positioning", "danger": 9,
             "confirm": ["no_audience_defined", "generic_claims", "everyone_is_target", "low_conversion"],
             "rule_out": ["specific_niche", "measurable_promise", "clear_differentiator"]},
            {"dx": "wrong_target_audience", "category": "positioning", "danger": 8,
             "confirm": ["message_resonates_nobody", "high_churn", "wrong_inquiries"],
             "rule_out": ["known_icp", "high_qualified_leads", "strong_niche"]},
            {"dx": "me_too_positioning", "category": "positioning", "danger": 7,
             "confirm": ["competitors_say_same_thing", "price_only_differentiator", "commodity_market"],
             "rule_out": ["unique_product", "patent", "exclusive_process", "strong_brand_recognition"]},
            {"dx": "premature_niche_narrowing", "category": "positioning", "danger": 3,
             "confirm": ["startup_early_stage", "unproven_market", "first_customers_not_yet"],
             "rule_out": ["established_demand", "existing_customer_base", "market_proven"]},
        ],
        "brand_messaging": [
            {"dx": "inconsistent_voice", "category": "messaging", "danger": 8,
             "confirm": ["multiple_authors", "no_guidelines", "different_tone_everywhere"],
             "rule_out": ["brand_guide_exists", "single_content_owner", "consistent_feedback"]},
            {"dx": "too_complex_message", "category": "messaging", "danger": 7,
             "confirm": ["confused_customer_responses", "low_engagement", "long_explanation_needed"],
             "rule_out": ["simple_product", "niche_expert_audience", "high_comprehension"]},
            {"dx": "too_generic_message", "category": "messaging", "danger": 6,
             "confirm": ["no_emotional_hook", "feature_list_only", "sounds_like_competitors"],
             "rule_out": ["strong_distinctive_story", "emotional_resonance_confirmed"]},
            {"dx": "wrong_language_register", "category": "messaging", "danger": 4,
             "confirm": ["audience_mismatch_feedback", "high_bounce_rate", "formal_brand_informal_audience"],
             "rule_out": ["audience_research_done", "strong_engagement_metrics"]},
        ],
        "brand_identity": [
            {"dx": "no_coherent_identity", "category": "identity", "danger": 8,
             "confirm": ["multiple_logos", "ad_hoc_design", "no_guidelines", "inconsistent_across_channels"],
             "rule_out": ["brand_guide_exists", "single_designer", "consistent_visual_feedback"]},
            {"dx": "identity_misaligned_with_positioning", "category": "identity", "danger": 7,
             "confirm": ["premium_brand_with_cheap_design", "startup_look_for_enterprise_target"],
             "rule_out": ["recent_rebrand", "audience_research_aligned"]},
            {"dx": "outdated_identity", "category": "identity", "danger": 5,
             "confirm": ["created_over_five_years_ago", "competitors_look_more_modern", "brand_feels_old"],
             "rule_out": ["recent_refresh", "timeless_design_style"]},
            {"dx": "over_engineered_identity", "category": "identity", "danger": 3,
             "confirm": ["too_many_elements", "unusable_in_small_format", "team_cant_apply_it"],
             "rule_out": ["clear_primary_mark", "simple_core_system"]},
        ],
        "brand_awareness": [
            {"dx": "wrong_channels", "category": "awareness", "danger": 8,
             "confirm": ["low_engagement_all_channels", "audience_active_elsewhere", "mismatched_platform"],
             "rule_out": ["channel_research_done", "strong_engagement_on_channel"]},
            {"dx": "inconsistent_presence", "category": "awareness", "danger": 7,
             "confirm": ["sporadic_posts", "no_content_plan", "appears_and_disappears"],
             "rule_out": ["editorial_calendar_exists", "regular_cadence", "content_team_in_place"]},
            {"dx": "insufficient_reach", "category": "awareness", "danger": 6,
             "confirm": ["new_brand", "low_follower_count", "zero_press_coverage"],
             "rule_out": ["niche_high_value_audience", "strong_referral_base", "targeted_outreach"]},
            {"dx": "no_earned_media", "category": "awareness", "danger": 5,
             "confirm": ["no_pr_activity", "no_referrals", "no_organic_buzz"],
             "rule_out": ["pr_program_active", "community_engaged"]},
        ],
        "brand_trust": [
            {"dx": "no_proof_of_expertise", "category": "trust", "danger": 9,
             "confirm": ["no_case_studies", "no_testimonials", "generic_claims_only"],
             "rule_out": ["strong_portfolio", "published_references", "thought_leadership_content"]},
            {"dx": "negative_online_reputation", "category": "trust", "danger": 8,
             "confirm": ["bad_reviews_visible", "social_complaints", "negative_press"],
             "rule_out": ["strong_rating", "no_negative_reviews_found", "proactive_reputation_management"]},
            {"dx": "authority_mismatch", "category": "trust", "danger": 6,
             "confirm": ["positioned_as_expert_but_shallow_content", "no_thought_leadership"],
             "rule_out": ["published_articles", "speaking_engagements", "deep_content_library"]},
            {"dx": "no_human_behind_brand", "category": "trust", "danger": 4,
             "confirm": ["no_about_page", "no_founder_story", "faceless_brand"],
             "rule_out": ["founder_visible", "strong_about_section", "personal_brand_active"]},
        ],
        "growth_strategy": [
            {"dx": "brand_funnel_disconnected", "category": "growth", "danger": 8,
             "confirm": ["ads_not_matching_brand", "mixed_messages_at_touchpoints", "no_integrated_strategy"],
             "rule_out": ["integrated_brand_strategy", "consistent_customer_journey"]},
            {"dx": "value_proposition_unclear_to_buyer", "category": "growth", "danger": 8,
             "confirm": ["long_sales_cycle", "frequent_price_objections", "prospects_dont_get_it"],
             "rule_out": ["clear_value_articulated", "strong_conversion_rate"]},
            {"dx": "wrong_segment_targeted", "category": "growth", "danger": 7,
             "confirm": ["lots_of_leads_no_conversion", "wrong_budget_fit", "mismatched_expectations"],
             "rule_out": ["high_qualified_lead_ratio", "clear_icp_defined"]},
            {"dx": "retention_problem_not_acquisition", "category": "growth", "danger": 6,
             "confirm": ["high_churn", "no_referrals", "customers_not_returning"],
             "rule_out": ["low_churn", "strong_nps", "active_referral_program"]},
        ],
        "competitive_pressure": [
            {"dx": "undifferentiated_in_crowded_market", "category": "competition", "danger": 9,
             "confirm": ["many_competitors_same_offer", "price_only_differentiator", "no_clear_reason_to_choose_us"],
             "rule_out": ["unique_product", "exclusive_process", "strong_brand_recognition"]},
            {"dx": "disruptive_new_entrant", "category": "competition", "danger": 8,
             "confirm": ["new_competitor_appeared", "price_disruption", "market_share_dropping"],
             "rule_out": ["loyal_customer_base", "high_switching_costs", "brand_moat"]},
            {"dx": "being_copied", "category": "competition", "danger": 7,
             "confirm": ["competitors_copying_positioning", "brand_dilution", "confusion_in_market"],
             "rule_out": ["strong_ip", "first_mover_advantage", "distinctive_brand_elements"]},
            {"dx": "commodity_trap", "category": "competition", "danger": 6,
             "confirm": ["competing_only_on_price", "customer_not_loyal_to_brand", "price_sensitive_market"],
             "rule_out": ["brand_premium_commanded", "loyal_repeat_customers"]},
        ],
    }

    def __init__(self, config: PolicyConfig):
        self.config = config

    def evaluate_turn(
        self,
        *,
        tenant_id: str,
        session_id: str,
        turn_id: str,
        user_text: str,
        history: List[Turn],
        flow_config: Optional[Dict[str, Any]] = None,
        previous_context: Optional[Dict[str, Any]] = None,
    ) -> ConversationPolicyDecision:
        if not self.config.enable_strategic_policy:
            return ConversationPolicyDecision(enabled=False)

        policy_mode = self._resolve_policy_mode(flow_config or {})
        text_lower = user_text.lower().strip()
        if policy_mode == "clinical":
            return self._evaluate_clinical_turn(
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=turn_id,
                user_text=user_text,
                text_lower=text_lower,
                history=history,
                previous_context=previous_context,
            )
        return self._evaluate_sales_turn(
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            user_text=user_text,
            text_lower=text_lower,
            history=history,
            previous_context=previous_context,
        )

    def _resolve_policy_mode(self, flow_config: Dict[str, Any]) -> str:
        configured = str(
            flow_config.get("policy_mode")
            or flow_config.get("conversation_policy")
            or self.config.policy_mode_default
        ).strip().lower()
        return configured if configured in {"sales", "clinical"} else self.config.policy_mode_default

    def _evaluate_sales_turn(
        self,
        *,
        tenant_id: str,
        session_id: str,
        turn_id: str,
        user_text: str,
        text_lower: str,
        history: List[Turn],
        previous_context: Optional[Dict[str, Any]] = None,
    ) -> ConversationPolicyDecision:
        stage = "sales.discovery"
        response_act = "probe"
        retrieval_profile = "sales_discovery"
        # Carry forward hypotheses from the previous turn so findings accumulate
        hypotheses: List[str] = list(previous_context.get("hypotheses", [])) if previous_context else []
        missing_slots: List[str] = []
        observations: List[StructuredObservation] = []

        if any(keyword in text_lower for keyword in self.SALES_CLOSE_KEYWORDS):
            stage = "sales.close"
            response_act = "confirm"
            retrieval_profile = "sales_close"
        elif any(keyword in text_lower for keyword in self.SALES_OBJECTION_KEYWORDS):
            stage = "sales.objection"
            response_act = "clarify"
            retrieval_profile = "sales_objection"
            hypotheses.append("user_has_adoption_or_cost_objection")
        elif len(history) <= 2:
            stage = "sales.discovery"
            response_act = "probe"
            retrieval_profile = "sales_discovery"

        company_match = re.search(r"\b(?:company|firma|marke|agency|agentur)\s+(?:is|heißt|named|called)?\s*([A-Z][\w&.\- ]{1,40})", user_text)
        if company_match:
            company = company_match.group(1).strip(" .,:;")
            observations.append(
                StructuredObservation(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    doc_type="Case_Memory",
                    topic="sales_company_signal",
                    confidence=0.82,
                    issue=f"Prospect company identified as {company}.",
                    solution=f"Use {company} as the canonical company reference in follow-up questions.",
                    metadata={"company": company, "policy_mode": "sales", "stage": stage},
                )
            )

        if "phone" in text_lower or "telefon" in text_lower:
            missing_slots.append("phone_confirmation")
        if "email" in text_lower:
            missing_slots.append("email_confirmation")

        if "problem" in text_lower or "pain" in text_lower or "issue" in text_lower:
            observations.append(
                StructuredObservation(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    doc_type="Case_Memory",
                    topic="sales_pain_signal",
                    confidence=0.7,
                    issue=user_text.strip(),
                    solution="Use this pain point to personalize the next qualifying question.",
                    metadata={"policy_mode": "sales", "stage": stage},
                )
            )

        return ConversationPolicyDecision(
            enabled=True,
            policy_mode="sales",
            conversation_stage=stage,
            response_act=response_act,
            hypotheses=hypotheses,
            missing_slots=missing_slots,
            retrieval_profile=retrieval_profile,
            memory_write_candidates=observations,
            metadata={"turn_kind": "sales", "history_turns": len(history)},
        )

    def _evaluate_clinical_turn(
        self,
        *,
        tenant_id: str,
        session_id: str,
        turn_id: str,
        user_text: str,
        text_lower: str,
        history: List[Turn],
        previous_context: Optional[Dict[str, Any]] = None,
    ) -> ConversationPolicyDecision:
        stage = "clinical.intake"
        response_act = "probe"
        retrieval_profile = "clinical_intake"
        # Carry forward state accumulated across turns
        prior_hypotheses: List[str] = list(previous_context.get("hypotheses", [])) if previous_context else []
        prior_missing: List[str] = list(previous_context.get("missing_slots", [])) if previous_context else []
        prior_confirmed_dx: List[str] = list(previous_context.get("confirmed_dx", [])) if previous_context else []
        prior_ruled_out_dx: List[str] = list(previous_context.get("ruled_out_dx", [])) if previous_context else []
        hypotheses: List[str] = list(prior_hypotheses)
        missing_slots: List[str] = []
        observations: List[StructuredObservation] = []

        symptom_candidates = [term for term in self.CLINICAL_SYMPTOMS if term in text_lower]
        negative_findings = [term for term in self.CLINICAL_NEGATIVE_CUES if term in text_lower]
        duration_value = self._extract_duration(text_lower)
        severity_value = self._extract_first_match(text_lower, self.CLINICAL_SEVERITY_TERMS)
        trigger_value = self._extract_first_match(text_lower, self.CLINICAL_TRIGGER_TERMS)
        onset_value = self._extract_onset(text_lower)
        contradictions = self._extract_contradictions(text_lower)

        # Differential scoring — hypothetico-deductive model
        ranked_diffs = self._score_differentials(
            symptom_candidates=symptom_candidates,
            negative_findings=negative_findings,
            prior_ruled_out=prior_ruled_out_dx,
        )

        # Build hypothesis list from ranked differentials (danger ≥ 3 get surfaced)
        for entry in ranked_diffs:
            label = f"{entry['category']}:{entry['dx']}:danger={entry['danger']}"
            if label not in hypotheses:
                hypotheses.append(label)

        if symptom_candidates:
            missing_slots.extend(self._build_missing_slots(
                onset_value=onset_value,
                duration_value=duration_value,
                severity_value=severity_value,
                trigger_value=trigger_value,
                negative_findings=negative_findings,
            ))
            observations.append(
                StructuredObservation(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    doc_type="Case_Memory",
                    topic="clinical_symptom_signal",
                    confidence=0.84,
                    issue=f"Patient reported: {', '.join(symptom_candidates[:3])}.",
                    solution="Use ranked differentials to ask the most discriminating next question.",
                    metadata={
                        "symptoms": symptom_candidates[:3],
                        "negative_findings": negative_findings[:4],
                        "onset": onset_value,
                        "duration": duration_value,
                        "severity": severity_value,
                        "trigger": trigger_value,
                        "ranked_differentials": [
                            {"dx": d["dx"], "danger": d["danger"]} for d in ranked_diffs[:3]
                        ],
                        "policy_mode": "clinical",
                        "stage": stage,
                    },
                )
            )

        if any(flag in text_lower for flag in self.CLINICAL_RED_FLAGS):
            stage = "clinical.triage"
            response_act = "escalate"
            retrieval_profile = "clinical_red_flag"
            if "potential_red_flag_condition" not in hypotheses:
                hypotheses.insert(0, "potential_red_flag_condition")
            missing_slots = []
            observations.append(
                StructuredObservation(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    doc_type="Case_Memory",
                    topic="clinical_red_flag",
                    confidence=0.95,
                    issue=f"Potential red-flag presentation: {user_text.strip()}",
                    solution="Prioritize urgent triage guidance and avoid low-acuity reassurance.",
                    metadata={"policy_mode": "clinical", "stage": stage, "red_flag": True},
                )
            )
        elif len(history) > 3 and symptom_candidates and len(self._build_missing_slots(
            onset_value=onset_value,
            duration_value=duration_value,
            severity_value=severity_value,
            trigger_value=trigger_value,
            negative_findings=negative_findings,
        )) <= 1:
            stage = "clinical.summary"
            response_act = "summarize"
            retrieval_profile = "clinical_summary"

        # Merge prior missing_slots: keep slots not filled this turn
        filled_this_turn: set[str] = set()
        if onset_value:
            filled_this_turn.add("onset")
        if duration_value:
            filled_this_turn.add("duration")
        if severity_value:
            filled_this_turn.add("severity")
        if trigger_value:
            filled_this_turn.add("trigger")
        if negative_findings:
            filled_this_turn.add("negative_findings")
        for slot in prior_missing:
            if slot not in filled_this_turn and slot not in missing_slots:
                missing_slots.append(slot)

        # De-duplicate hypotheses while preserving order
        seen_hyp: set[str] = set()
        deduped_hypotheses: List[str] = []
        for h in hypotheses:
            if h not in seen_hyp:
                seen_hyp.add(h)
                deduped_hypotheses.append(h)
        hypotheses = deduped_hypotheses

        if contradictions:
            observations.append(
                StructuredObservation(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    doc_type="Case_Memory",
                    topic="clinical_contradiction",
                    confidence=0.68,
                    issue=f"Potential contradiction or nuance: {user_text.strip()}",
                    solution="Reflect the contradiction back and clarify before narrowing the hypothesis.",
                    metadata={"policy_mode": "clinical", "stage": stage, "contradictions": contradictions[:3]},
                )
            )

        active_summary = self._build_active_listening_summary(
            symptoms=symptom_candidates,
            onset_value=onset_value,
            duration_value=duration_value,
            severity_value=severity_value,
            trigger_value=trigger_value,
            negative_findings=negative_findings,
        )
        next_question_focus = self._choose_next_question_focus(
            stage=stage,
            symptom_candidates=symptom_candidates,
            missing_slots=missing_slots,
            negative_findings=negative_findings,
            ranked_differentials=ranked_diffs,
        )
        if next_question_focus:
            # Keep focus as first element but don't duplicate
            focus_label = f"focus:{next_question_focus}"
            hypotheses = [focus_label] + [h for h in hypotheses if not h.startswith("focus:")]

        return ConversationPolicyDecision(
            enabled=True,
            policy_mode="clinical",
            conversation_stage=stage,
            response_act=response_act,
            hypotheses=hypotheses[:5],
            missing_slots=missing_slots,
            retrieval_profile=retrieval_profile,
            memory_write_candidates=observations,
            ranked_differentials=ranked_diffs,
            confirmed_dx=prior_confirmed_dx,
            ruled_out_dx=prior_ruled_out_dx,
            metadata={
                "turn_kind": "clinical",
                "history_turns": len(history),
                "active_listening_summary": active_summary,
                "negative_findings": negative_findings[:4],
                "next_question_focus": next_question_focus,
                "contradictions": contradictions[:3],
            },
        )

    @staticmethod
    def _extract_first_match(text_lower: str, candidates: tuple[str, ...]) -> Optional[str]:
        for candidate in candidates:
            if candidate in text_lower:
                return candidate
        return None

    def _extract_duration(self, text_lower: str) -> Optional[str]:
        """Extract timeline / urgency from brand engagement context."""
        for pattern, unit in self.CLINICAL_DURATION_PATTERNS:
            match = re.search(pattern, text_lower)
            if match:
                groups = match.groups()
                value = groups[0] if groups else ""
                return f"{value}_{unit}" if value else unit
        return None

    @staticmethod
    def _extract_onset(text_lower: str) -> Optional[str]:
        """Extract what triggered / prompted the brand engagement."""
        trigger_markers = {
            "just started": "new_company",
            "new company": "new_company",
            "startup": "startup",
            "new brand": "new_brand",
            "rebranding": "rebrand",
            "rebrand": "rebrand",
            "new product": "new_product",
            "neues produkt": "new_product",
            "pivot": "pivot",
            "funding": "funding",
            "finanzierung": "funding",
            "investor": "investor_moment",
            "merger": "merger",
            "fusion": "merger",
            "scaling": "growth_phase",
            "skalierung": "growth_phase",
            "growing": "growth_phase",
            "wachsen": "growth_phase",
        }
        for marker, normalized in trigger_markers.items():
            if marker in text_lower:
                return normalized
        return None

    @staticmethod
    def _extract_contradictions(text_lower: str) -> List[str]:
        contradiction_markers = ["but", "however", "although", "aber", "jedoch", "trotzdem"]
        return [marker for marker in contradiction_markers if marker in text_lower]

    @staticmethod
    def _build_missing_slots(
        *,
        onset_value: Optional[str],
        duration_value: Optional[str],
        severity_value: Optional[str],
        trigger_value: Optional[str],
        negative_findings: List[str],
    ) -> List[str]:
        """Brand engagement slots: what we still need to understand."""
        missing_slots: List[str] = []
        if not onset_value:
            missing_slots.append("brand_trigger")      # what prompted the need
        if not duration_value:
            missing_slots.append("timeline")           # urgency / deadline
        if not severity_value:
            missing_slots.append("urgency_level")      # how critical is this
        if not trigger_value:
            missing_slots.append("context_trigger")    # growth / funding / launch
        if not negative_findings:
            missing_slots.append("existing_assets")    # what do they already have
        return missing_slots

    @staticmethod
    def _build_active_listening_summary(
        *,
        symptoms: List[str],
        onset_value: Optional[str],
        duration_value: Optional[str],
        severity_value: Optional[str],
        trigger_value: Optional[str],
        negative_findings: List[str],
    ) -> str:
        parts: List[str] = []
        if symptoms:
            parts.append(f"brand problem area: {', '.join(symptoms[:3])}")
        if onset_value:
            parts.append(f"context: {onset_value}")
        if duration_value:
            parts.append(f"timeline: {duration_value}")
        if severity_value:
            parts.append(f"urgency: {severity_value}")
        if trigger_value:
            parts.append(f"trigger: {trigger_value}")
        if negative_findings:
            parts.append(f"confirmed assets: {', '.join(negative_findings[:3])}")
        return "; ".join(parts)

    def _score_differentials(
        self,
        *,
        symptom_candidates: List[str],
        negative_findings: List[str],
        prior_ruled_out: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Score differentials using hypothetico-deductive model.

        Returns ranked list of differential candidates with effective danger scores.
        """
        ruled_out: set[str] = set(prior_ruled_out or [])
        candidate_map: Dict[str, Dict[str, Any]] = {}

        for symptom in symptom_candidates:
            dx_key = self.SYMPTOM_TO_DX_KEY.get(symptom)
            if not dx_key or dx_key not in self.DIFFERENTIAL_MAP:
                continue
            for entry in self.DIFFERENTIAL_MAP[dx_key]:
                dx_id = f"{entry['category']}:{entry['dx']}"
                if dx_id in ruled_out:
                    continue
                if dx_id not in candidate_map or candidate_map[dx_id]["danger"] < entry["danger"]:
                    candidate_map[dx_id] = {
                        "dx": entry["dx"],
                        "category": entry["category"],
                        "danger": entry["danger"],
                        "confirm_needed": list(entry["confirm"]),
                        "rule_out_features": list(entry["rule_out"]),
                    }

        # Apply negative findings: if a negative finding matches a ruling-out criterion,
        # reduce danger score and mark candidate as more likely ruled out
        for neg in negative_findings:
            neg_words = set(neg.replace(" ", "_").split("_"))
            for dx_id, entry in list(candidate_map.items()):
                for ro_feature in entry["rule_out_features"]:
                    ro_words = set(ro_feature.split("_"))
                    if neg_words & ro_words:
                        entry["danger"] = max(0, entry["danger"] - 4)

        ranked = sorted(candidate_map.values(), key=lambda x: x["danger"], reverse=True)
        return ranked[:5]

    @staticmethod
    def _choose_next_question_focus(
        *,
        stage: str,
        symptom_candidates: List[str],
        missing_slots: List[str],
        negative_findings: List[str],
        ranked_differentials: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        if stage == "clinical.triage":
            return "red_flag_screen"

        ranked = ranked_differentials or []

        # If we have differentials, use them to pick a discriminating question
        if ranked:
            top = ranked[0]
            second = ranked[1] if len(ranked) > 1 else None

            # High-danger: ask the most important confirm question first
            if top["danger"] >= 8 and top["confirm_needed"]:
                confirm_key = top["confirm_needed"][0]
                return f"dx_confirm:{top['dx']}:{confirm_key}"

            # Two viable differentials: ask a question that differentiates them
            if second and second["danger"] >= 3 and top["confirm_needed"] and second["rule_out_features"]:
                for q in top["confirm_needed"]:
                    q_words = set(q.split("_"))
                    for ro in second["rule_out_features"]:
                        ro_words = set(ro.split("_"))
                        if q_words & ro_words:
                            return f"dx_discriminate:{top['dx']}vs{second['dx']}:{q}"
                # Fall through to probing the top dx
                return f"dx_probe:{top['dx']}:{top['confirm_needed'][0]}"

            # Single differential: probe its most important confirm feature
            if top["confirm_needed"]:
                return f"dx_probe:{top['dx']}:{top['confirm_needed'][0]}"

        # Slot-based fallback when no differentials are available
        if "severity" in missing_slots:
            return "severity"
        if "onset" in missing_slots:
            return "onset"
        if "duration" in missing_slots:
            return "duration"
        if "trigger" in missing_slots:
            return "trigger"
        if "negative_findings" in missing_slots and symptom_candidates:
            return f"dx_probe:general:negative_findings"
        if negative_findings:
            return "clarify_pattern"
        return "summary"
