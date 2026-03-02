"""
TARA Dialogue Quality Evaluator

Runs 20 Telugu(+Indic) dialogue test cases through ContextArchitect prompt assembly,
gets model responses, then uses a judge LLM to validate formatting/register/behavior checks.

Usage:
  cd rag-daytona.v2
  export GROQ_API_KEY=...
  python3 test_tara_dialogue_eval.py \
      --model openai/gpt-oss-120b \
      --judge-model qwen/qwen3-32b \
      --output tara_dialogue_eval_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from groq import AsyncGroq

from context_architecture import ContextArchitect


TEST_CASES: List[Dict[str, Any]] = [
    {
        "id": 1,
        "category": "greeting",
        "description": "Simple hi — tests default persona warmth",
        "query": "hi",
        "raw_query": "hi",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Responds warmly in Telugu+English",
            "Short first sentence (6-10 words)",
            "No markup, no bullets",
            "Ends with a question to continue conversation",
        ],
    },
    {
        "id": 2,
        "category": "greeting",
        "description": "Casual Hyderabadi opener in Telugu",
        "query": "ఏంటి సంగతి",
        "raw_query": "ఏంటి సంగతి",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Replies in same casual Hyderabadi register",
            "Mirrors user energy — casual, not formal",
            "Short and punchy, not lecture-style",
            "No banned phrases like 'క్షమించండి' or 'మార్గనిర్దేశనం'",
        ],
    },
    {
        "id": 3,
        "category": "career",
        "description": "Job opportunities query — tests postposition spacing",
        "query": "నాకు job కావాలి",
        "raw_query": "నాకు job కావాలి",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "'job' stays in English (not translated)",
            "Postpositions spaced: 'job లో' or 'career కి' NOT 'jobలో' 'careerకి'",
            "Asks clarifying question: what type, what field",
            "Mentions TASK resources naturally",
            "No bullet points",
        ],
    },
    {
        "id": 4,
        "category": "career",
        "description": "Resume improvement — key spacing test",
        "query": "resume ఎలా improve చేసుకోవాలి",
        "raw_query": "resume ఎలా improve చేసుకోవాలి",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "'resume లో' NOT 'resumeలో'",
            "Ideas in prose, NOT numbered bullets",
            "Mentions: structure, action verbs, achievements",
            "Offers to review their resume",
            "Max 4-5 sentences",
        ],
    },
    {
        "id": 5,
        "category": "career",
        "description": "Interview preparation ask",
        "query": "interview కి ఎలా prepare అవ్వాలి",
        "raw_query": "interview కి ఎలా prepare అవ్వాలి",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "'interview కి' properly spaced",
            "Practical advice in prose",
            "Mentions TASK mock interview resources",
            "Encouraging tone, not lecture",
            "Ends with question or offer",
        ],
    },
    {
        "id": 6,
        "category": "empathy",
        "description": "Interview failure — deep empathy test",
        "query": "మళ్ళీ interview fail అయింది",
        "raw_query": "మళ్ళీ interview fail అయింది",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Empathy sentence FIRST before any advice",
            "Does NOT immediately jump to tips",
            "Warm, genuine tone — not robotic sympathy",
            "Short sentences throughout",
            "Reminds them they are not alone",
        ],
    },
    {
        "id": 7,
        "category": "empathy",
        "description": "User feeling overwhelmed and anxious",
        "query": "చాలా overwhelmed గా feel అవుతున్నా, ఏం చేయాలో తెలియట్లేదు",
        "raw_query": "చాలా overwhelmed గా feel అవుతున్నా, ఏం చేయాలో తెలియట్లేదు",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Calm, reassuring tone — not cheerful or dismissive",
            "Does NOT immediately give a list of tips",
            "Acknowledges the feeling first",
            "Asks what specifically is overwhelming",
            "Short, grounding sentences",
        ],
    },
    {
        "id": 8,
        "category": "empathy",
        "description": "Celebrating a win — tests excitement register",
        "query": "నాకు internship select అయింది!",
        "raw_query": "నాకు internship select అయింది!",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Genuinely celebratory, not flat",
            "Uses exclamation naturally (not overused)",
            "Asks follow-up about the internship",
            "Keeps it conversational, not formal congratulations",
        ],
    },
    {
        "id": 9,
        "category": "technical",
        "description": "Code error help — tests technical English in Telugu prose",
        "query": "నా Python code లో error వస్తుంది",
        "raw_query": "నా Python code లో error వస్తుంది",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "'code లో' properly spaced",
            "Technical terms (Python, error, indentation, debug) stay in English",
            "Asks for the error message or code",
            "Curious, investigative tone",
            "Short sentences",
        ],
    },
    {
        "id": 10,
        "category": "technical",
        "description": "Multi-turn tech debug — tests history continuation",
        "query": "ఇది నా error: NameError: name 'x' is not defined",
        "raw_query": "ఇది నా error: NameError: name 'x' is not defined",
        "detected_language": "telugu",
        "history": [
            {"role": "user", "content": "నా Python code లో error వస్తుంది", "timestamp": "10:01"},
            {"role": "assistant", "content": "Okay, చూద్దాం. Error message ఏం వస్తుందో paste చేయండి.", "timestamp": "10:01"},
        ],
        "check": [
            "References previous conversation naturally",
            "Correctly diagnoses NameError (variable not defined or out of scope)",
            "Explains in Telugu+English mixed naturally",
            "Short, clear fix suggestion",
            "No repeated boilerplate from turn 1",
        ],
    },
    {
        "id": 11,
        "category": "technical",
        "description": "Learning resource ask — tests URL/platform handling",
        "query": "Python ఎక్కడ నేర్చుకోవాలి free గా",
        "raw_query": "Python ఎక్కడ నేర్చుకోవాలి free గా",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Platform names in English: Coursera, YouTube, LeetCode (not transliterated)",
            "No URLs embedded mid-sentence — spoken naturally",
            "No bullet list — prose only",
            "Offers to send link",
            "Short sentences with natural pauses",
        ],
    },
    {
        "id": 12,
        "category": "task_info",
        "description": "What is TASK — tests org name in Telugu script",
        "query": "TASK అంటే ఏంటి",
        "raw_query": "TASK అంటే ఏంటి",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Uses 'టాస్క్' in Telugu script (NOT just 'TASK' in Roman)",
            "Full form mentioned once then dropped",
            "Short sentences, no unnecessary detail",
            "Ends with offer for more info",
        ],
    },
    {
        "id": 13,
        "category": "task_info",
        "description": "Asking for mentor — tests balu reference",
        "query": "నాకు mentor కావాలి career కోసం",
        "raw_query": "నాకు mentor కావాలి career కోసం",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Mentions mentor connection is available",
            "Mentions 'బాలు గారు' if referencing senior mentor",
            "Asks what kind of career guidance they need first",
            "Warm and helpful tone",
        ],
    },
    {
        "id": 14,
        "category": "identity",
        "description": "Who built you — tests identity clarity",
        "query": "నిన్ను ఎవరు build చేశారు",
        "raw_query": "నిన్ను ఎవరు build చేశారు",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Says 'Davinci AI' clearly",
            "Does NOT say OpenAI, Anthropic, Google",
            "Stays in persona — doesn't break character",
            "Brief and confident answer",
        ],
    },
    {
        "id": 15,
        "category": "identity",
        "description": "Are you human or AI — tests persona consistency",
        "query": "నువ్వు human ఆ AI ఆ",
        "raw_query": "నువ్వు human ఆ AI ఆ",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Stays in persona as TARA",
            "Does not say 'I am just an AI'",
            "Warm, confident deflection",
            "Redirects to helping",
        ],
    },
    {
        "id": 16,
        "category": "edge_case",
        "description": "Very short vague message — tests clarification handling",
        "query": "help",
        "raw_query": "help",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "Does NOT assume what help is needed",
            "Asks open-ended question warmly",
            "Short response",
            "Does not give a menu of options as bullets",
        ],
    },
    {
        "id": 17,
        "category": "edge_case",
        "description": "Mixed language input (Telugu + English sentence)",
        "query": "నాకు data science లో career start చేయాలని ఉంది but ఎక్కడ start చేయాలో తెలియట్లేదు",
        "raw_query": "నాకు data science లో career start చేయాలని ఉంది but ఎక్కడ start చేయాలో తెలియట్లేదు",
        "detected_language": "telugu",
        "history": [],
        "check": [
            "'data science లో' properly spaced",
            "Matches user's mixed input energy",
            "Gives practical starting point in prose",
            "Asks about their current skill level",
            "Mentions TASK resources naturally",
        ],
    },
    {
        "id": 18,
        "category": "multi_turn",
        "description": "Follow-up after career discussion — tests context continuity",
        "query": "okay so నేను ఏం చేయాలి ఇప్పుడు",
        "raw_query": "okay so నేను ఏం చేయాలి ఇప్పుడు",
        "detected_language": "telugu",
        "history": [
            {"role": "user", "content": "నాకు data science లో career start చేయాలని ఉంది", "timestamp": "11:00"},
            {"role": "assistant", "content": "Data science లో start చేయడానికి first Python మరియు statistics basics కావాలి. మీకు ఇప్పుడు ఏ background ఉందో చెప్పండి.", "timestamp": "11:00"},
            {"role": "user", "content": "నాకు basic Python తెలుసు", "timestamp": "11:01"},
            {"role": "assistant", "content": "Perfect, Python base ఉంది కదా. Next step గా pandas మరియు numpy నేర్చుకోండి. Kaggle లో beginner datasets తో practice start చేయండి.", "timestamp": "11:01"},
        ],
        "check": [
            "Picks up exactly from where the conversation left off",
            "Does NOT repeat earlier advice",
            "Gives a concrete immediate next action",
            "References 'pandas' or 'Kaggle' naturally from prior context",
        ],
    },
    {
        "id": 19,
        "category": "multi_turn",
        "description": "Sudden topic switch mid-conversation — tests graceful pivot",
        "query": "actually forget it, నాకు resume help కావాలి",
        "raw_query": "actually forget it, నాకు resume help కావాలి",
        "detected_language": "telugu",
        "history": [
            {"role": "user", "content": "data science career start చేయాలని ఉంది", "timestamp": "11:00"},
            {"role": "assistant", "content": "Data science లో start చేయడానికి Python basics కావాలి.", "timestamp": "11:00"},
        ],
        "check": [
            "Acknowledges topic switch naturally, not awkwardly",
            "Does NOT say 'Sure, I understand you want to change topic'",
            "Pivots smoothly to resume help",
            "Asks for their current resume or field",
        ],
    },
    {
        "id": 20,
        "category": "language_switch",
        "description": "User switches to Hindi mid-conversation — tests seamless language switch",
        "query": "yaar mujhe interview ki preparation karni hai",
        "raw_query": "yaar mujhe interview ki preparation karni hai",
        "detected_language": "hindi",
        "history": [
            {"role": "user", "content": "నాకు job కావాలి", "timestamp": "12:00"},
            {"role": "assistant", "content": "అరే, ఏ type job కావాలి? మీ field చెప్పండి.", "timestamp": "12:00"},
        ],
        "check": [
            "Switches to Hinglish seamlessly — no comment about switching",
            "Does NOT say 'I notice you switched to Hindi'",
            "Casual Hinglish register: 'arre', 'dekho', 'batao'",
            "NOT Doordarshan-style formal Hindi",
            "Short sentences, continues naturally from job context",
        ],
    },
]


@dataclass
class UsageStats:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


def _extract_cached_tokens(usage: Any) -> int:
    try:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None and isinstance(usage, dict):
            details = usage.get("prompt_tokens_details")
        if details is None:
            return 0
        if isinstance(details, dict):
            return int(details.get("cached_tokens", 0) or 0)
        return int(getattr(details, "cached_tokens", 0) or 0)
    except Exception:
        return 0


def _usage_from_completion(completion: Any) -> UsageStats:
    u = getattr(completion, "usage", None)
    if not u:
        return UsageStats()
    return UsageStats(
        prompt_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(u, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(u, "total_tokens", 0) or 0),
        cached_tokens=_extract_cached_tokens(u),
    )


def _split_prompt_for_chat(prompt: str) -> List[Dict[str, str]]:
    if "<zone_a_system_configuration>" in prompt:
        parts = prompt.split("</zone_a_system_configuration>", 1)
        system_content = parts[0].replace("<zone_a_system_configuration>", "").strip()
        user_content = parts[1].strip() if len(parts) > 1 else ""
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]
    return [{"role": "user", "content": prompt}]


def _rough_format_flags(text: str) -> List[str]:
    issues: List[str] = []
    if re.search(r"^\s*[-*•]\s+", text, flags=re.MULTILINE):
        issues.append("Bullet formatting detected")
    if re.search(r"^\s*\d+\.\s+", text, flags=re.MULTILINE):
        issues.append("Numbered list formatting detected")
    if re.search(r"<[^>]+>", text):
        issues.append("Markup/tag detected")
    if "..." in text:
        issues.append("Ellipsis detected (discouraged for TTS rhythm)")
    if re.search(r"[A-Za-z]+లో|[A-Za-z]+కి|[A-Za-z]+తో|[A-Za-z]+లో", text):
        issues.append("Possible missing space between English word and Telugu postposition")
    return issues


async def _generate_response(
    client: AsyncGroq,
    model: str,
    prompt: str,
    timeout_s: float,
    max_completion_tokens: int,
) -> Tuple[str, UsageStats]:
    messages = _split_prompt_for_chat(prompt)
    kwargs: Dict[str, Any] = dict(
        messages=messages,
        model=model,
        temperature=0.6,
        stop=["</resp>", "</turn>", "</ctxt>"],
    )
    if "gpt-oss" in model.lower():
        kwargs["max_completion_tokens"] = max_completion_tokens
        kwargs["reasoning_effort"] = "low"
        kwargs["include_reasoning"] = False
    else:
        kwargs["max_tokens"] = max_completion_tokens

    completion = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=timeout_s)
    content = (completion.choices[0].message.content or "").strip()
    usage = _usage_from_completion(completion)
    return content, usage


async def _judge_response(
    client: AsyncGroq,
    judge_model: str,
    test_case: Dict[str, Any],
    response_text: str,
    timeout_s: float,
    judge_max_tokens: int,
) -> Dict[str, Any]:
    judge_input = {
        "test_id": test_case["id"],
        "category": test_case["category"],
        "description": test_case["description"],
        "query": test_case["raw_query"],
        "detected_language": test_case["detected_language"],
        "history": test_case.get("history", []),
        "checks": test_case["check"],
        "response": response_text,
    }

    system = (
        "You are a strict dialogue QA judge for multilingual TTS assistants.\n"
        "Evaluate the assistant response against each check.\n"
        "Return ONLY JSON with fields:\n"
        "overall_pass (bool), pass_count (int), fail_count (int),\n"
        "checks (array of {check, pass, reason}),\n"
        "format_issues (array string), register_issues (array string),\n"
        "suggestions (array string), confidence (0..1).\n"
        "Flag formatting issues: bullets, numbered lists, markup/tags, bad code-mix spacing, robotic register."
    )
    user = json.dumps(judge_input, ensure_ascii=False)

    kwargs: Dict[str, Any] = dict(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=judge_model,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    if "gpt-oss" in judge_model.lower():
        kwargs["max_completion_tokens"] = judge_max_tokens
        kwargs["include_reasoning"] = False
        kwargs["reasoning_effort"] = "low"
    else:
        kwargs["max_tokens"] = judge_max_tokens

    try:
        completion = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=timeout_s)
        raw = completion.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        parsed["_judge_usage"] = _usage_from_completion(completion).__dict__
        return parsed
    except Exception as e:
        # Fallback: do not fail the whole test run if judge JSON mode fails.
        return {
            "overall_pass": False,
            "pass_count": 0,
            "fail_count": len(test_case.get("check", [])),
            "checks": [{"check": c, "pass": False, "reason": "Judge JSON validation failed"} for c in test_case.get("check", [])],
            "format_issues": ["Judge failed to return valid JSON"],
            "register_issues": [],
            "suggestions": ["Retry judge with higher max tokens or without strict JSON mode"],
            "confidence": 0.0,
            "judge_error": str(e),
            "_judge_usage": {},
        }


async def run(args: argparse.Namespace) -> None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is required")

    client = AsyncGroq(api_key=api_key)
    selected = TEST_CASES[: args.max_cases] if args.max_cases > 0 else TEST_CASES

    report: Dict[str, Any] = {
        "run_at_epoch": int(time.time()),
        "model": args.model,
        "judge_model": args.judge_model,
        "total_cases": len(selected),
        "results": [],
        "totals": {
            "cases_passed": 0,
            "cases_failed": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "judge_prompt_tokens": 0,
            "judge_completion_tokens": 0,
            "judge_cached_tokens": 0,
        },
    }

    for tc in selected:
        prompt = ContextArchitect.assemble_prompt(
            query=tc["query"],
            raw_query=tc["raw_query"],
            retrieved_docs=[],
            history=tc.get("history", []),
            hive_mind={"insights": {}},
            user_profile={"language": tc.get("detected_language", "auto")},
            agent_skills=[],
            agent_rules=[],
            detected_language=tc.get("detected_language"),
        )

        response_text, usage = await _generate_response(
            client, args.model, prompt, args.timeout, args.max_completion_tokens
        )
        rough_issues = _rough_format_flags(response_text)
        judge = await _judge_response(
            client, args.judge_model, tc, response_text, args.timeout, args.judge_max_tokens
        )

        case_pass = bool(judge.get("overall_pass", False))
        if case_pass:
            report["totals"]["cases_passed"] += 1
        else:
            report["totals"]["cases_failed"] += 1

        report["totals"]["prompt_tokens"] += usage.prompt_tokens
        report["totals"]["completion_tokens"] += usage.completion_tokens
        report["totals"]["cached_tokens"] += usage.cached_tokens

        judge_usage = judge.get("_judge_usage", {})
        report["totals"]["judge_prompt_tokens"] += int(judge_usage.get("prompt_tokens", 0) or 0)
        report["totals"]["judge_completion_tokens"] += int(judge_usage.get("completion_tokens", 0) or 0)
        report["totals"]["judge_cached_tokens"] += int(judge_usage.get("cached_tokens", 0) or 0)

        result_row = {
            "id": tc["id"],
            "category": tc["category"],
            "description": tc["description"],
            "query": tc["raw_query"],
            "response": response_text,
            "model_usage": usage.__dict__,
            "heuristic_format_issues": rough_issues,
            "judge": judge,
        }
        report["results"].append(result_row)

        status = "PASS" if case_pass else "FAIL"
        cache_rate = (usage.cached_tokens / usage.prompt_tokens * 100.0) if usage.prompt_tokens else 0.0
        print(
            f"[{status}] #{tc['id']:02d} {tc['category']} | "
            f"tokens p/c={usage.prompt_tokens}/{usage.completion_tokens} cached={usage.cached_tokens} ({cache_rate:.1f}%)"
        )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    total = report["total_cases"]
    passed = report["totals"]["cases_passed"]
    print(f"\nDone: {passed}/{total} passed")
    print(f"Report: {args.output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run 20 TARA dialogue test cases with LLM judge validation.")
    parser.add_argument("--model", default="openai/gpt-oss-120b", help="Model under test")
    parser.add_argument("--judge-model", default="qwen/qwen3-32b", help="Judge model")
    parser.add_argument("--output", default="tara_dialogue_eval_report.json", help="Output report JSON")
    parser.add_argument("--max-cases", type=int, default=0, help="Run first N cases (0=all)")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout seconds per LLM call")
    parser.add_argument("--max-completion-tokens", type=int, default=1024, help="Max completion tokens for model under test")
    parser.add_argument("--judge-max-tokens", type=int, default=900, help="Max completion tokens for judge model")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
