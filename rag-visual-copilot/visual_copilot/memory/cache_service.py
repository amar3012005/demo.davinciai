from typing import Any, Optional


class CacheService:
    @staticmethod
    def _schema_cache(state: Any) -> dict:
        if not hasattr(state, "_schema_cache"):
            state._schema_cache = {}
        return state._schema_cache

    @staticmethod
    def _hive_cache(state: Any) -> dict:
        if not hasattr(state, "_hive_cache"):
            state._hive_cache = {}
        return state._hive_cache

    @classmethod
    def get_schema(cls, state: Any, session_id: str, goal: str) -> Optional[Any]:
        entry = cls._schema_cache(state).get(session_id)
        if not entry:
            return None
        if entry.get("goal") != goal:
            return None
        return entry.get("schema")

    @classmethod
    def set_schema(cls, state: Any, session_id: str, goal: str, schema: Any) -> None:
        cls._schema_cache(state)[session_id] = {"goal": goal, "schema": schema}

    @classmethod
    def get_hive(cls, state: Any, session_id: str) -> Optional[Any]:
        return cls._hive_cache(state).get(session_id)

    @classmethod
    def set_hive(cls, state: Any, session_id: str, hive_response: Any) -> None:
        cls._hive_cache(state)[session_id] = hive_response
