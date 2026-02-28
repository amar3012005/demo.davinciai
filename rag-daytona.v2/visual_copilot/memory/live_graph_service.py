from typing import Any, List


class LiveGraphService:
    def __init__(self, live_graph: Any):
        self._live_graph = live_graph

    async def get_visible_nodes(self, session_id: str) -> List[Any]:
        return await self._live_graph.get_visible_nodes(session_id)
