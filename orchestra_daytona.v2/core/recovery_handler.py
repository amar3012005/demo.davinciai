"""
Recovery Handler Module - WebSocket handlers for session recovery and resume.

This module adds recovery/resume functionality to the WebSocket handler.
It should be imported and integrated into ws_handler.py.

Provides:
- _handle_resume_session: Handle client resume requests
- Recovery state update hooks for mission events
- Action ledger integration
"""

import json
import logging
import time
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class RecoveryHandlerMixin:
    """
    Mixin class providing recovery/resume handlers for WebSocket handler.
    
    Usage:
        class OrchestratorWSHandler(RecoveryHandlerMixin, BaseHandler):
            ...
    """
    
    # These attributes should exist on the parent class
    redis_client: Any = None
    sessions: Dict[str, Any] = {}
    
    def get_recovery_store(self):
        """Get RecoveryStore instance (lazy import to avoid circular deps)"""
        from core.recovery_store import RecoveryStore
        return RecoveryStore(self.redis_client)
    
    def get_action_ledger(self):
        """Get ActionLedger instance"""
        from core.action_ledger import ActionLedger
        return ActionLedger(self.redis_client)
    
    def get_pipeline_resume(self):
        """Get PipelineResume instance"""
        from core.pipeline_resume import PipelineResume
        return PipelineResume(self.redis_client)

    async def _resolve_page_node_payload(
        self,
        current_url: str,
        page_title: str = "",
        dom_elements: Optional[List[Dict[str, Any]]] = None,
        existing_page_node: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        if not getattr(getattr(self, "config", None), "feature_flags", None) or not self.config.feature_flags.pageindex_enabled:
            return existing_page_node.to_dict() if hasattr(existing_page_node, "to_dict") else existing_page_node
        if existing_page_node:
            if hasattr(existing_page_node, "to_dict"):
                return existing_page_node.to_dict()
            if isinstance(existing_page_node, dict):
                return existing_page_node

        if not current_url:
            return None

        try:
            from visual_copilot.navigation import get_page_index, get_page_locator

            page_index = get_page_index()
            domain = (urlparse(current_url).netloc or "").replace("www.", "")
            if domain and domain not in page_index.get_all_domains():
                await page_index.load_domain_map(domain)

            static_node = page_index.resolve_current_node(current_url, domain=domain or None)
            if static_node:
                return {
                    "node_id": static_node.node_id,
                    "logical_path": static_node.logical_path,
                    "source": "static_map",
                    "url_pattern": static_node.path_regex,
                    "current_url": current_url,
                    "expected_controls": static_node.expected_controls,
                    "parent_node_id": static_node.parent_node_id,
                    "title": static_node.title,
                    "summary": static_node.summary_of_contents,
                    "capabilities": static_node.terminal_capabilities,
                }

            locator = get_page_locator()
            dynamic_node = await locator.resolve_page_node(
                url=current_url,
                title=page_title,
                dom_elements=dom_elements,
                static_node=None,
            )
            return dynamic_node.to_dict()
        except Exception as e:
            logger.debug(f"Failed to resolve page node payload: {e}")
            return None

    async def _sync_session_from_recovery_state(self, session: Any, recovery_state: Any, current_url: str = ""):
        session.current_goal = recovery_state.goal
        session.current_url = recovery_state.current_url or current_url
        session.is_mission_active = recovery_state.status in ("in_progress", "paused")
        session.goal_step_count = int(recovery_state.step_count or 0)
        session.metadata["recovery_state_loaded"] = True
        if recovery_state.mission_id:
            session.mission_id = recovery_state.mission_id
    
    async def _handle_resume_session(self, session: Any, msg: Dict[str, Any]):
        """
        Handle resume_session message from frontend.
        
        Frontend sends this after reload/reconnect to recover mission state.
        
        Expected message format:
        {
            "type": "resume_session",
            "session_id": "abc123",
            "current_url": "https://example.com/page",
            "page_title": "Page Title",
            "goal_hint": "Optional goal hint from frontend storage",
            "client_context": {
                "step_count": 5,  # Frontend's recollection (ignored if backend has state)
                "subgoal_index": 2,
                "action_history": [...]  # Frontend's recollection (for reference only)
            }
        }
        
        Response:
        {
            "type": "resume_state",
            "session_id": "abc123",
            "mission_id": "m-001",
            "goal": "Find models on GroqCloud",
            "phase": "strategy",  # or "last_mile" or "done"
            "step_count": 5,  # Backend's authoritative count
            "subgoal_index": 2,
            "page_node": {...},  # Current page node from site map
            "recent_actions": [...],  # Last 5 actions for context
            "has_pending_pipeline": false,
            "resume_count": 1
        }
        """
        session_id = session.session_id
        logger.info(f"[{session_id}] 🔄 Resume session requested")
        
        try:
            # Extract resume parameters
            current_url = msg.get("current_url", "")
            page_title = msg.get("page_title", "")
            goal_hint = msg.get("goal_hint", "")
            
            # Load recovery state from Redis
            recovery_store = self.get_recovery_store()
            recovery_state = await recovery_store.load_recovery_state(session_id=session_id)
            
            if not recovery_state:
                logger.warning(f"[{session_id}] No recovery state found for session")
                
                # Send negative response
                await self._send_json(session.websocket, {
                    "type": "resume_state",
                    "session_id": session_id,
                    "found": False,
                    "message": "No active mission found for this session"
                }, session)
                return
            
            # Recovery state found - this is the authoritative source
            logger.info(
                f"[{session_id}] ✅ Recovery state found | "
                f"mission={recovery_state.mission_id} | "
                f"phase={recovery_state.phase} | "
                f"step={recovery_state.step_count} | "
                f"url={recovery_state.current_url}"
            )
            
            # Load recent actions from ledger
            action_ledger = self.get_action_ledger()
            recent_actions = await action_ledger.get_recent_actions(
                session_id, limit=5
            )
            recent_actions_data = [a.to_dict() for a in recent_actions]
            
            # Check for pending pipeline
            pipeline_resume = self.get_pipeline_resume()
            has_pending_pipeline = await pipeline_resume.has_pending_pipeline(session_id)
            
            # Resolve current page node
            page_node = await self._resolve_page_node_payload(
                current_url=current_url or recovery_state.current_url,
                page_title=page_title,
                existing_page_node=recovery_state.page_node,
            )
            if page_node and not recovery_state.page_node:
                recovery_state.page_node = page_node
            
            # Increment resume count
            recovery_state.resume_count += 1
            await recovery_store.save_recovery_state(recovery_state)
            
            # Update session with recovered state
            await self._sync_session_from_recovery_state(session, recovery_state, current_url=current_url)
            
            # Send resume state response
            await self._send_json(session.websocket, {
                "type": "resume_state",
                "session_id": session_id,
                "found": True,
                "mission_id": recovery_state.mission_id,
                "mission_status": recovery_state.status,
                "goal": recovery_state.goal,
                "phase": recovery_state.phase,
                "status": recovery_state.status,
                "step_count": recovery_state.step_count,
                "subgoal_index": recovery_state.subgoal_index,
                "page_node": page_node,
                "current_url": recovery_state.current_url,
                "recent_actions": recent_actions_data,
                "pending_pipeline": recovery_state.pending_pipeline,
                "has_pending_pipeline": has_pending_pipeline,
                "resume_count": recovery_state.resume_count,
                "resume_instruction": "request_dom_refresh" if recovery_state.status in ("in_progress", "paused") else "noop",
            }, session)
            
            logger.info(
                f"[{session_id}] 📤 Resume state sent | "
                f"mission={recovery_state.mission_id} | "
                f"actions={len(recent_actions_data)} | "
                f"resume_count={recovery_state.resume_count}"
            )
            
        except Exception as e:
            logger.error(f"[{session_id}] Resume session failed: {e}", exc_info=True)
            
            # Send error response
            await self._send_json(session.websocket, {
                "type": "resume_state",
                "session_id": session_id,
                "found": False,
                "error": str(e)
            }, session)
    
    async def _update_recovery_on_mission_started(
        self,
        session: Any,
        mission_id: str,
        goal: str,
        initial_url: str
    ):
        """
        Update recovery state when a mission starts.
        
        Call this when a new mission is initiated.
        """
        try:
            from core.recovery_store import RecoveryState, RecoveryStatus, MissionPhase
            
            recovery_store = self.get_recovery_store()
            
            # Create initial recovery state
            state = RecoveryState(
                session_id=session.session_id,
                mission_id=mission_id,
                goal=goal,
                status=RecoveryStatus.IN_PROGRESS.value,
                phase=MissionPhase.STRATEGY.value,
                step_count=0,
                subgoal_index=0,
                current_url=initial_url
            )
            
            await recovery_store.save_recovery_state(state)
            
            # Initialize action ledger with empty state
            action_ledger = self.get_action_ledger()
            # Ledger auto-initializes on first append
            
            logger.info(
                f"[{session.session_id}] 💾 Recovery state initialized | "
                f"mission={mission_id} | goal={goal[:50]}..."
            )
            
        except Exception as e:
            logger.error(f"Failed to initialize recovery state: {e}", exc_info=True)

    async def _update_recovery_dom_state(
        self,
        session: Any,
        *,
        current_url: str,
        dom_signature: str = "",
        page_title: str = "",
        dom_elements: Optional[List[Dict[str, Any]]] = None,
    ):
        try:
            recovery_store = self.get_recovery_store()
            state = await recovery_store.load_recovery_state(
                session_id=session.session_id,
                mission_id=getattr(session, "mission_id", None),
            )
            if not state:
                return

            state.current_url = current_url or state.current_url
            state.latest_dom_signature = dom_signature or state.latest_dom_signature
            state.last_stable_dom_at = time.time()
            page_node = await self._resolve_page_node_payload(
                current_url=state.current_url,
                page_title=page_title,
                dom_elements=dom_elements,
                existing_page_node=state.page_node,
            )
            if page_node:
                state.page_node = page_node
            await recovery_store.save_recovery_state(state)
        except Exception as e:
            logger.error(f"Failed to update recovery DOM state: {e}", exc_info=True)
    
    async def _update_recovery_on_action_planned(
        self,
        session: Any,
        pipeline_id: str,
        action_type: str,
        target_id: str,
        label: str,
        url_before: str
    ):
        """
        Update action ledger when an action is planned.
        
        Call this when the planner generates a new action.
        """
        try:
            from core.action_ledger import ActionRecord, ActionStatus
            
            action_ledger = self.get_action_ledger()
            
            # Get next sequence number
            recent = await action_ledger.get_recent_actions(session.session_id, limit=1)
            seq = (recent[0].seq + 1) if recent else 1
            
            # Create action record
            action = ActionRecord(
                seq=seq,
                pipeline_id=pipeline_id,
                type=action_type,
                target_id=target_id,
                label=label,
                status=ActionStatus.PLANNED.value,
                url_before=url_before
            )
            
            await action_ledger.append_action(session.session_id, action)
            
            # Increment step count in recovery state
            recovery_store = self.get_recovery_store()
            await recovery_store.increment_step_count(session.session_id)
            
            logger.debug(
                f"[{session.session_id}] 📝 Action planned | "
                f"seq={seq} | type={action_type} | target={target_id}"
            )
            
        except Exception as e:
            logger.error(f"Failed to record planned action: {e}", exc_info=True)
    
    async def _update_recovery_on_action_sent(
        self,
        session: Any,
        seq: int
    ):
        """
        Update action status when sent to frontend.
        """
        try:
            from core.action_ledger import ActionStatus
            
            action_ledger = self.get_action_ledger()
            await action_ledger.update_action_status(
                session.session_id, seq, ActionStatus.SENT
            )
            
            logger.debug(f"[{session.session_id}] Action {seq} sent")
            
        except Exception as e:
            logger.error(f"Failed to update action status: {e}", exc_info=True)
    
    async def _update_recovery_on_action_executed(
        self,
        session: Any,
        seq: int,
        url_after: str
    ):
        """
        Update action status when executed by frontend.
        """
        try:
            from core.action_ledger import ActionStatus
            
            action_ledger = self.get_action_ledger()
            await action_ledger.update_action_status(
                session.session_id, seq, ActionStatus.EXECUTED,
                url_after=url_after
            )
            
            logger.debug(f"[{session.session_id}] Action {seq} executed")
            
        except Exception as e:
            logger.error(f"Failed to update action status: {e}", exc_info=True)
    
    async def _update_recovery_on_action_confirmed(
        self,
        session: Any,
        seq: int,
        url_after: str
    ):
        """
        Update action status when confirmed (DOM stabilized after execution).
        """
        try:
            from core.action_ledger import ActionStatus
            
            action_ledger = self.get_action_ledger()
            await action_ledger.update_action_status(
                session.session_id, seq, ActionStatus.CONFIRMED,
                url_after=url_after
            )
            
            logger.debug(f"[{session.session_id}] Action {seq} confirmed")
            
        except Exception as e:
            logger.error(f"Failed to update action status: {e}", exc_info=True)
    
    async def _update_recovery_on_action_failed(
        self,
        session: Any,
        seq: int,
        error: str
    ):
        """
        Update action status when failed.
        """
        try:
            from core.action_ledger import ActionStatus
            
            action_ledger = self.get_action_ledger()
            await action_ledger.update_action_status(
                session.session_id, seq, ActionStatus.FAILED,
                error=error
            )
            
            logger.warning(
                f"[{session.session_id}] ❌ Action {seq} failed: {error}"
            )
            
        except Exception as e:
            logger.error(f"Failed to update action status: {e}", exc_info=True)
    
    async def _update_recovery_on_mission_complete(
        self,
        session: Any,
        mission_id: str,
        success: bool = True
    ):
        """
        Update recovery state when mission completes.
        
        Call this when the mission is successfully completed or failed.
        """
        try:
            from core.recovery_store import RecoveryState, RecoveryStatus, MissionPhase
            
            recovery_store = self.get_recovery_store()
            
            # Load current state
            state = await recovery_store.load_recovery_state(
                session_id=session.session_id
            )
            
            if state:
                state.status = (
                    RecoveryStatus.COMPLETED.value if success
                    else RecoveryStatus.FAILED.value
                )
                state.phase = MissionPhase.DONE.value
                
                await recovery_store.save_recovery_state(state)
                
                # Clear pending pipeline
                pipeline_resume = self.get_pipeline_resume()
                await pipeline_resume.clear_pipeline(session.session_id)
                
                logger.info(
                    f"[{session.session_id}] 🏁 Mission complete | "
                    f"mission={mission_id} | success={success}"
                )
            
        except Exception as e:
            logger.error(f"Failed to update recovery on mission complete: {e}", exc_info=True)
    
    async def _update_recovery_on_phase_change(
        self,
        session: Any,
        new_phase: str,
        subgoal_index: Optional[int] = None
    ):
        """
        Update recovery state when mission phase changes.
        
        Call this when transitioning between strategy/last_mile phases.
        """
        try:
            from core.recovery_store import MissionPhase
            
            recovery_store = self.get_recovery_store()
            
            # Map string phase to enum
            phase_map = {
                "strategy": MissionPhase.STRATEGY,
                "last_mile": MissionPhase.LAST_MILE,
                "done": MissionPhase.DONE
            }
            phase = phase_map.get(new_phase, MissionPhase.STRATEGY)
            
            await recovery_store.update_phase(
                session.session_id, phase, subgoal_index
            )
            
            logger.debug(
                f"[{session.session_id}] Phase updated | "
                f"phase={new_phase} | subgoal={subgoal_index}"
            )
            
        except Exception as e:
            logger.error(f"Failed to update phase: {e}", exc_info=True)
    
    async def _update_recovery_on_navigation(
        self,
        session: Any,
        new_url: str,
        page_node: Optional[Any] = None
    ):
        """
        Update recovery state when navigation occurs.
        
        Call this when the agent navigates to a new page.
        """
        try:
            from core.recovery_store import PageNodeRef
            
            recovery_store = self.get_recovery_store()
            
            # Convert page_node to PageNodeRef if needed
            node_ref = None
            if page_node:
                if isinstance(page_node, dict):
                    node_ref = PageNodeRef.from_dict(page_node)
                else:
                    node_ref = page_node
            
            if node_ref:
                node_ref.current_url = new_url
                await recovery_store.update_page_node(session.session_id, node_ref)
            else:
                # Just update URL
                state = await recovery_store.load_recovery_state(session.session_id)
                if state:
                    state.current_url = new_url
                    await recovery_store.save_recovery_state(state)
            
            logger.debug(
                f"[{session.session_id}] Navigation recorded | url={new_url}"
            )
            
        except Exception as e:
            logger.error(f"Failed to update navigation: {e}", exc_info=True)
    
    async def _save_pending_pipeline(
        self,
        session: Any,
        pipeline_id: str,
        actions: List[Dict[str, Any]],
        last_acknowledged_index: int = -1,
        expected_navigation: Optional[Dict[str, Any]] = None
    ):
        """
        Save pending multi-action pipeline for recovery.
        
        Call this when a multi-action pipeline is generated.
        """
        try:
            from core.pipeline_resume import PipelineState, PipelineAction
            
            pipeline_resume = self.get_pipeline_resume()
            
            # Convert actions to PipelineAction objects
            pipeline_actions = []
            for i, action_spec in enumerate(actions):
                pipeline_actions.append(PipelineAction(
                    index=i,
                    type=action_spec.get("type", ""),
                    target_id=action_spec.get("target_id", ""),
                    label=action_spec.get("label", ""),
                    parameters=action_spec.get("parameters", {})
                ))
            
            # Create pipeline state
            pipeline = PipelineState(
                pipeline_id=pipeline_id,
                session_id=session.session_id,
                mission_id=session.current_goal or "",  # Use goal as mission_id proxy
                actions=pipeline_actions,
                last_acknowledged_index=last_acknowledged_index,
                expected_navigation=expected_navigation
            )
            
            await pipeline_resume.save_pipeline(pipeline)
            
            # Also update recovery state
            recovery_store = self.get_recovery_store()
            await recovery_store.set_pending_pipeline(
                session.session_id, pipeline.to_dict()
            )
            
            logger.info(
                f"[{session.session_id}] 💾 Pipeline saved | "
                f"pipeline={pipeline_id} | actions={len(actions)}"
            )
            
        except Exception as e:
            logger.error(f"Failed to save pipeline: {e}", exc_info=True)

    async def _handle_action_acknowledgements(
        self,
        session: Any,
        acknowledgements: List[Dict[str, Any]],
        current_url: str = "",
    ):
        if not acknowledgements:
            return

        try:
            action_ledger = self.get_action_ledger()
            pipeline_resume = self.get_pipeline_resume()
            recent_actions = await action_ledger.get_recent_actions(session.session_id, limit=10)
            actions_by_pipeline: Dict[str, List[Any]] = {}
            for action in recent_actions:
                actions_by_pipeline.setdefault(action.pipeline_id, []).append(action)
            for actions in actions_by_pipeline.values():
                actions.sort(key=lambda item: item.seq)

            for ack in acknowledgements:
                pipeline_id = ack.get("pipeline_id") or ""
                action_index = ack.get("action_index")
                executed = ack.get("executed", False)
                url_after = ack.get("url_after") or current_url

                if pipeline_id and isinstance(action_index, int):
                    await pipeline_resume.mark_action_complete(
                        session.session_id,
                        action_index=action_index,
                        error=None if executed else ack.get("error", "execution_failed"),
                    )

                    pipeline_actions = actions_by_pipeline.get(pipeline_id, [])
                    if 0 <= action_index < len(pipeline_actions):
                        target_action = pipeline_actions[action_index]
                        from core.action_ledger import ActionStatus
                        await action_ledger.update_action_status(
                            session.session_id,
                            seq=target_action.seq,
                            new_status=ActionStatus.CONFIRMED if executed else ActionStatus.FAILED,
                            url_after=url_after,
                            error=None if executed else ack.get("error", "execution_failed"),
                        )
            if acknowledgements:
                recovery_store = self.get_recovery_store()
                state = await recovery_store.load_recovery_state(session_id=session.session_id)
                if state:
                    pipeline = await pipeline_resume.load_pipeline(session.session_id)
                    state.pending_pipeline = pipeline.to_dict() if pipeline and not pipeline.is_complete() else None
                    await recovery_store.save_recovery_state(state)
                    if pipeline and pipeline.is_complete():
                        await pipeline_resume.clear_pipeline(session.session_id)
        except Exception as e:
            logger.error(f"Failed to handle action acknowledgements: {e}", exc_info=True)


# Helper function to integrate mixin into existing handler
def integrate_recovery_handlers(handler_instance: Any):
    """
    Integrate recovery handlers into an existing WebSocket handler instance.
    
    This adds the recovery methods to the handler so they can be called
    from _route_message.
    
    Usage:
        handler = OrchestratorWSHandler(...)
        integrate_recovery_handlers(handler)
    """
    method_names = [
        "get_recovery_store",
        "get_action_ledger",
        "get_pipeline_resume",
        "_resolve_page_node_payload",
        "_sync_session_from_recovery_state",
        "_handle_resume_session",
        "_update_recovery_on_mission_started",
        "_update_recovery_dom_state",
        "_update_recovery_on_action_planned",
        "_update_recovery_on_action_sent",
        "_update_recovery_on_action_executed",
        "_update_recovery_on_action_confirmed",
        "_update_recovery_on_action_failed",
        "_update_recovery_on_mission_complete",
        "_update_recovery_on_phase_change",
        "_update_recovery_on_navigation",
        "_save_pending_pipeline",
        "_handle_action_acknowledgements",
    ]
    for name in method_names:
        method = getattr(RecoveryHandlerMixin, name)
        setattr(handler_instance, name, method.__get__(handler_instance, handler_instance.__class__))
    
    logger.info("Recovery handlers integrated into WebSocket handler")
