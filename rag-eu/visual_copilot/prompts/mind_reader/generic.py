"""Generic Mind Reader prompt for unknown domains. Uses gpt-oss-20b reasoning model."""

from urllib.parse import urlparse


def _infer_site_profile(domain: str, current_url: str) -> str:
    d = (domain or "").lower()
    u = (current_url or "").lower()
    host = ""
    try:
        host = (urlparse(current_url).netloc or d).lower()
    except Exception:
        host = d

    shop_markers = [
        "myntra", "amazon", "flipkart", "ajio", "nike", "adidas", "zalando",
        "shop", "store", "cart", "checkout", "product"
    ]
    docs_markers = ["docs", "developer", "reference", "guide", "api", "sdk"]
    console_markers = ["console", "dashboard", "billing", "usage", "activity", "settings"]

    if any(m in host or m in u for m in shop_markers):
        return "shopping"
    if any(m in host or m in u for m in console_markers):
        return "console"
    if any(m in host or m in u for m in docs_markers):
        return "docs"
    return "generic"


def get_prompt(user_input: str, current_url: str, domain: str, previous_goal_section: str, nodes: list = None) -> str:
    # Build DOM context from live graph nodes
    nodes_context = "(no DOM elements available — predict based on common website patterns)"
    if nodes:
        interactive = [n for n in nodes if getattr(n, 'interactive', False)]
        # Prioritize: search inputs first, then nav links, then buttons
        inputs = [n for n in interactive if n.tag in ('input', 'textarea') or getattr(n, 'role', '') == 'searchbox']
        nav_links = [n for n in interactive if n.tag == 'a' and getattr(n, 'zone', '') in ('nav', 'sidebar')]
        buttons = [n for n in interactive if n.tag == 'button' or n.role == 'button']
        rest = [n for n in interactive if n not in inputs and n not in nav_links and n not in buttons]
        ordered = (inputs + nav_links + buttons + rest)[:40]

        nodes_str = "\n".join([
            f"[ID: {n.id}] tag={n.tag} zone={getattr(n, 'zone', '?')} text='{(n.text or '')[:40]}'"
            for n in ordered
        ])
        nodes_context = f"LIVE DOM ELEMENTS (prioritized: inputs > nav > buttons):\n{nodes_str}"

    site_profile = _infer_site_profile(domain=domain, current_url=current_url)
    profile_instruction = "General websites: infer the shortest safe next step."
    if site_profile == "shopping":
        profile_instruction = (
            "Shopping sites: decompose intent into human-like flow. "
            "For search intent, NEXT STEP must search product + color only. "
            "Do NOT include size in search query. Size belongs to filter step."
        )
    elif site_profile == "console":
        profile_instruction = (
            "Console/SaaS sites: prioritize navigation to Usage/Billing/Settings pages, "
            "then read or extract requested data."
        )
    elif site_profile == "docs":
        profile_instruction = (
            "Docs sites: first navigate to the right section/tab, then extract the answer."
        )

    return f"""You are TARA, an intelligent browser co-pilot. Parse the user's intent and produce a robust next-step plan from live DOM.

USER INPUT: "{user_input}"
CURRENT URL: {current_url}
DOMAIN: {domain}
SITE PROFILE: {site_profile}
{previous_goal_section}

{nodes_context}

PLANNING POLICY:
- {profile_instruction}
- Keep `overall_goal` elaborated and specific.
- Keep `next_subgoal` atomic and executable on current page.

1. CLEAN the input: Strip voice artifacts ("I asked", "show me", "can you find").

2. CLASSIFY the action:
   - "extraction" = view/read data already visible (stats, prices, info)
   - "navigation" = go to a different page/section
   - "search" = type keywords into a search box
   - "purchase" = buy/add to cart
   - "interaction" = click a button, toggle, select option

3. EXTRACT target_entity: The specific thing the user wants. Preserve ALL proper nouns, model names, brand names, product names exactly as spoken.

4. BUILD PLAN:
   - overall_goal: full human-like objective including required constraints.
   - planned_steps: short ordered steps (2-5).
   - next_subgoal: only the immediate next action.

5. PREDICT next_subgoal by MATCHING against the DOM elements above:
   - If there's a search <input> and the action is "search" → "Type 'keywords' in search [ID: xxx]"
   - On shopping profile, search keywords should be product + color only (example: "Blue denim jeans")
   - If there's a nav link matching the target → "Click on 'Link Text' [ID: xxx]"
   - If the target content is already visible → null (no action needed)
   - Use REAL [ID: xxx] from the DOM list above. Never invent IDs.
   - If no matching element found → null

6. FOLLOW-UP RESOLUTION: If previous_goal exists and input is short/contextual:
   - "in X" / "with X" / "size X" → merge with previous entity
   - "latest" / "cheapest" → add as filter/sort to previous entity

OUTPUT (strict JSON, no extra text):
{{
  "action": "extraction|navigation|search|purchase|interaction",
  "target_entity": "concrete target with all identifiers preserved",
  "navigation_hint": "which page/section/area to look in",
  "overall_goal": "elaborated goal in one sentence",
  "planned_steps": ["step1", "step2", "step3"],
  "next_subgoal": "Type 'X' in search [ID: actual-id]" or "Click on 'Y' [ID: actual-id]" or null,
  "first_subgoal": "same as next_subgoal",
  "domain": "{domain}",
  "constraints": {{}}
}}

EXAMPLES:

User: "find white nike shoes" | DOM has: [ID: search-input] tag=input text=''
Output: {{"action": "search", "target_entity": "white nike shoes", "navigation_hint": "search bar", "overall_goal": "Search white nike shoes, then apply relevant filters and review matching products.", "planned_steps": ["Type query in search", "Apply color filter", "Apply size filter if requested", "Review results"], "next_subgoal": "Type 'white nike shoes' in search [ID: search-input]", "first_subgoal": "Type 'white nike shoes' in search [ID: search-input]", "domain": "{domain}", "constraints": {{"color": "white"}}}}

User: "order me denim blue jeans with M size" | shopping profile | DOM has: [ID: search-input] tag=input text=''
Output: {{"action": "search", "target_entity": "denim jeans", "navigation_hint": "search bar and filter panel", "overall_goal": "Search denim jeans, choose blue color, choose size M, then review products before purchase.", "planned_steps": ["Search for denim jeans", "Apply blue color filter", "Apply size M filter", "Review product cards"], "next_subgoal": "Type 'Blue denim jeans' in search [ID: search-input]", "first_subgoal": "Type 'Blue denim jeans' in search [ID: search-input]", "domain": "{domain}", "constraints": {{"color": "blue", "size": "M"}}}}

User: "go to my account settings" | DOM has: [ID: nav-settings] tag=a text='Settings'
Output: {{"action": "navigation", "target_entity": "account settings", "navigation_hint": "Settings link", "overall_goal": "Open account settings page.", "planned_steps": ["Click settings link", "Wait for settings page"], "next_subgoal": "Click on 'Settings' [ID: nav-settings]", "first_subgoal": "Click on 'Settings' [ID: nav-settings]", "domain": "{domain}", "constraints": {{}}}}

User: "how much does the premium plan cost" | DOM has: [ID: pricing-link] tag=a text='Pricing'
Output: {{"action": "extraction", "target_entity": "premium plan pricing", "navigation_hint": "Pricing page", "overall_goal": "Navigate to pricing and extract premium plan cost.", "planned_steps": ["Open Pricing page", "Read premium plan price"], "next_subgoal": "Click on 'Pricing' [ID: pricing-link]", "first_subgoal": "Click on 'Pricing' [ID: pricing-link]", "domain": "{domain}", "constraints": {{}}}}

User: "check my order history" | No matching DOM element
Output: {{"action": "extraction", "target_entity": "order history", "navigation_hint": "Account or Orders page", "overall_goal": "Open orders area and read order history.", "planned_steps": ["Open account/orders page", "Read order entries"], "next_subgoal": null, "first_subgoal": null, "domain": "{domain}", "constraints": {{}}}}
"""
