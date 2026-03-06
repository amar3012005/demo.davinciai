from typing import Any

from visual_copilot.models.contracts import PlanningContext


class BootstrapError(RuntimeError):
    pass


def build_context(**kwargs: Any) -> PlanningContext:
    return PlanningContext(**kwargs)


def require_runtime_modules(app: Any) -> dict:
    required = {
        "mind_reader": getattr(app.state, "mind_reader", None),
        "hive_interface": getattr(app.state, "hive_interface", None),
        "mission_brain": getattr(app.state, "mission_brain", None),
        "semantic_detective": getattr(app.state, "semantic_detective", None),
        "live_graph": getattr(app.state, "live_graph", None),
    }
    missing = [k for k, v in required.items() if v is None]
    if missing:
        raise BootstrapError(f"missing_runtime_modules={','.join(missing)}")
    return required
