"""Compatibility wrapper for Visual CoPilot mind-reader generic prompt."""

from visual_copilot.prompts.mind_reader.generic import get_prompt as _vc_get_prompt


def get_prompt(user_input: str, current_url: str, domain: str, previous_goal_section: str, nodes: list = None) -> str:
    return _vc_get_prompt(
        user_input=user_input,
        current_url=current_url,
        domain=domain,
        previous_goal_section=previous_goal_section,
        nodes=nodes,
    )
