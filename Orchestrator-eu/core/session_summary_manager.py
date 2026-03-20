import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from core.history_manager import HistoryManager, Turn
from core.service_client import RAGClient

logger = logging.getLogger(__name__)


class SessionSummaryManager:
    """Redis-backed layered text session summary owned by the orchestrator."""

    def __init__(self, redis_client, rag_client: RAGClient, ttl_seconds: int = 60 * 60 * 12):
        self.redis = redis_client
        self.rag_client = rag_client
        self.ttl_seconds = ttl_seconds
        self._tasks: Dict[str, asyncio.Task] = {}
        self._pending_updates: Dict[str, List[Dict[str, Any]]] = {}

    @staticmethod
    def _summary_key(tenant_id: Optional[str], session_id: str) -> str:
        tenant = (tenant_id or "unknown").strip() or "unknown"
        return f"eu:{tenant}:session:{session_id}:summary:v1"

    @staticmethod
    def _lock_key(tenant_id: Optional[str], session_id: str) -> str:
        tenant = (tenant_id or "unknown").strip() or "unknown"
        return f"eu:{tenant}:session:{session_id}:summary:lock"

    @classmethod
    def _worker_key(cls, tenant_id: Optional[str], session_id: str) -> str:
        tenant = (tenant_id or "unknown").strip() or "unknown"
        return f"{tenant}:{session_id}"

    @staticmethod
    def _serialize_recent_turns(turns: List[Turn]) -> List[Dict[str, Any]]:
        return [
            {
                "role": turn.role,
                "content": turn.text,
                "timestamp": turn.timestamp,
                "metadata": turn.metadata or {},
            }
            for turn in turns
            if turn.text.strip()
        ]

    async def get_summary(
        self,
        tenant_id: Optional[str],
        session_id: str,
    ) -> Tuple[str, int]:
        if not self.redis:
            return "", 0
        try:
            raw = await self.redis.get(self._summary_key(tenant_id, session_id))
            if not raw:
                return "", 0
            data = json.loads(raw)
            return str(data.get("summary_text") or "").strip(), int(data.get("revision") or 0)
        except Exception as exc:
            logger.warning(f"[{session_id}] Failed reading session summary: {exc}")
            return "", 0

    async def get_prompt_payload(
        self,
        tenant_id: Optional[str],
        session_id: str,
        history_manager: HistoryManager,
    ) -> Dict[str, Any]:
        summary_text, revision = await self.get_summary(tenant_id, session_id)
        if not summary_text:
            recent_turns = history_manager.get_recent_turns(count=2)
        elif revision < 3:
            recent_turns = history_manager.get_recent_turns(count=2)
        else:
            recent_turns = history_manager.get_recent_turns(count=1)
        return {
            "session_summary_window": summary_text,
            "session_summary_revision": revision,
            "recent_turns": self._serialize_recent_turns(recent_turns),
        }

    def schedule_update(
        self,
        *,
        tenant_id: Optional[str],
        session_id: str,
        language: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        if not self.redis or not assistant_text.strip() or not user_text.strip():
            return

        worker_key = self._worker_key(tenant_id, session_id)
        queue = self._pending_updates.setdefault(worker_key, [])
        queue.append({
            "tenant_id": tenant_id,
            "session_id": session_id,
            "language": language,
            "user_text": user_text,
            "assistant_text": assistant_text,
        })

        existing = self._tasks.get(worker_key)
        if existing and not existing.done():
            return

        self._tasks[worker_key] = asyncio.create_task(self._run_worker(worker_key))

    async def _run_worker(self, worker_key: str) -> None:
        try:
            while True:
                queue = self._pending_updates.get(worker_key) or []
                if not queue:
                    self._pending_updates.pop(worker_key, None)
                    return
                payload = queue.pop(0)
                await self._update_summary(**payload)
        finally:
            self._tasks.pop(worker_key, None)

    async def _update_summary(
        self,
        *,
        tenant_id: Optional[str],
        session_id: str,
        language: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        lock_key = self._lock_key(tenant_id, session_id)
        token = f"{time.time()}:{id(asyncio.current_task())}"
        try:
            acquired = False
            for _ in range(6):
                acquired = await self.redis.set(lock_key, token, ex=20, nx=True)
                if acquired:
                    break
                await asyncio.sleep(0.2)
            if not acquired:
                logger.warning(f"[{session_id}] Summary update skipped after lock retries")
                return

            previous_summary, previous_revision = await self.get_summary(tenant_id, session_id)
            summary_text = await self.rag_client.summarize_session_window(
                session_id=session_id,
                tenant_id=tenant_id or "tara",
                language=language,
                previous_summary=previous_summary,
                user_text=user_text,
                assistant_text=assistant_text,
            )
            if not summary_text.strip():
                return

            payload = {
                "summary_text": summary_text.strip(),
                "revision": previous_revision + 1,
                "updated_at": time.time(),
                "tenant_id": tenant_id or "",
                "session_id": session_id,
                "language": language,
            }
            await self.redis.set(
                self._summary_key(tenant_id, session_id),
                json.dumps(payload, ensure_ascii=False),
                ex=self.ttl_seconds,
            )
            logger.info(
                f"[{session_id}] 🧠 Session summary updated "
                f"(rev={payload['revision']}, chars={len(payload['summary_text'])})"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[{session_id}] Session summary update failed: {exc}")
        finally:
            try:
                current = await self.redis.get(lock_key)
                if current == token:
                    await self.redis.delete(lock_key)
            except Exception:
                pass
