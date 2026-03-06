"""
Unit tests for FSM Routing Endpoint

Tests the /api/v1/fsm/route endpoint logic for schema-driven appointment booking.
"""

import pytest
import asyncio
from typing import Dict, Any


class TestFSMRouteCancel:
    """Test cancel detection"""
    
    @pytest.mark.asyncio
    async def test_cancel_keyword_explicit(self, default_fsm_context):
        """Test explicit cancel keyword detection"""
        from rag_engine import RAGEngine
        
        # Mock engine with minimal config
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="I want to cancel",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] == "cancel"
        assert result["cancelled"] is True
        assert result["confidence"] >= 0.85
    
    @pytest.mark.asyncio
    async def test_cancel_keyword_stop(self, default_fsm_context):
        """Test 'stop' keyword detection"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="stop please",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] == "cancel"
        assert result["cancelled"] is True
    
    @pytest.mark.asyncio
    async def test_cancel_keyword_nevermind(self, default_fsm_context):
        """Test 'nevermind' keyword detection"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="nevermind, I don't want to book",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] == "cancel"
        assert result["cancelled"] is True
    
    @pytest.mark.asyncio
    async def test_no_cancel_in_normal_text(self, default_fsm_context):
        """Test that normal text doesn't trigger cancel"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="My name is John Smith",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] != "cancel"
        assert result["cancelled"] is False


class TestFSMRouteFieldAnswer:
    """Test field answer classification"""
    
    @pytest.mark.asyncio
    async def test_valid_name_answer(self, default_fsm_context):
        """Test valid name answer"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="John Smith",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] in ("collect_field", "confirm_field")
        assert result["field"] == "name"
        assert result["normalized_value"] == "John Smith"
        assert result["confidence"] >= 0.8
    
    @pytest.mark.asyncio
    async def test_spelled_name_answer(self, default_fsm_context):
        """Test spelled-out name answer"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="J-O-H-N S-M-I-T-H",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] in ("collect_field", "confirm_field")
        assert result["field"] == "name"
        assert result["normalized_value"] == "Johnsmith"  # Letters combined
        assert result["confidence"] >= 0.8
    
    @pytest.mark.asyncio
    async def test_valid_email_answer(self, default_fsm_context):
        """Test valid email answer"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        default_fsm_context["pending_field"] = "email"
        
        result = await engine.route_fsm_turn(
            user_text="john@gmail.com",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] in ("collect_field", "confirm_field")
        assert result["field"] == "email"
        assert result["normalized_value"] == "john@gmail.com"
        assert result["confidence"] >= 0.9
    
    @pytest.mark.asyncio
    async def test_spelled_email_answer(self, default_fsm_context):
        """Test spelled-out email answer"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        default_fsm_context["pending_field"] = "email"
        
        result = await engine.route_fsm_turn(
            user_text="J-O-H-N at G-M-A-I-L dot C-O-M",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] in ("collect_field", "confirm_field")
        assert result["field"] == "email"
        assert result["normalized_value"] == "john@gmail.com"
        assert result["confidence"] >= 0.9
    
    @pytest.mark.asyncio
    async def test_invalid_email_format(self, default_fsm_context):
        """Test invalid email format"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        default_fsm_context["pending_field"] = "email"
        
        result = await engine.route_fsm_turn(
            user_text="notanemail",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        # Should not be a valid field answer
        assert result["action"] != "collect_field"
    
    @pytest.mark.asyncio
    async def test_valid_topic_answer(self, default_fsm_context):
        """Test valid topic answer"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        default_fsm_context["pending_field"] = "topic"
        
        result = await engine.route_fsm_turn(
            user_text="I need help with Daytona installation",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] in ("collect_field", "confirm_field")
        assert result["field"] == "topic"
        assert "Daytona installation" in result["normalized_value"]
        assert result["confidence"] >= 0.8


class TestFSMRouteDetour:
    """Test RAG detour detection"""
    
    @pytest.mark.asyncio
    async def test_general_question_detour(self, default_fsm_context):
        """Test general question triggers detour"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="What is Daytona?",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] == "detour_rag"
        assert result["resume_prompt"] is not None
        assert "Back to booking" in result["resume_prompt"]
    
    @pytest.mark.asyncio
    async def test_how_question_detour(self, default_fsm_context):
        """Test 'how' question triggers detour"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="How does the pricing work?",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] == "detour_rag"
        assert result["resume_prompt"] is not None
    
    @pytest.mark.asyncio
    async def test_help_request_detour(self, default_fsm_context):
        """Test help request triggers detour"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="Can you help me with something else first?",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] == "detour_rag"


class TestFSMRouteInvalidRetry:
    """Test invalid retry fallback"""
    
    @pytest.mark.asyncio
    async def test_gibberish_input(self, default_fsm_context):
        """Test gibberish input triggers invalid retry"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="12345 !!!",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] == "invalid_retry"
        assert result["resume_prompt"] is not None
    
    @pytest.mark.asyncio
    async def test_too_short_name(self, default_fsm_context):
        """Test too short name triggers invalid retry"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="A",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        # Should not be valid (too short)
        assert result["action"] != "collect_field"


class TestFSMRouteResumePrompt:
    """Test resume prompt generation"""
    
    @pytest.mark.asyncio
    async def test_resume_prompt_for_name(self, default_fsm_context):
        """Test resume prompt for name field"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        
        result = await engine.route_fsm_turn(
            user_text="What is Daytona?",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] == "detour_rag"
        assert result["resume_prompt"] is not None
        assert "name" in result["resume_prompt"].lower()
    
    @pytest.mark.asyncio
    async def test_resume_prompt_for_email(self, default_fsm_context):
        """Test resume prompt for email field"""
        from rag_engine import RAGEngine
        
        engine = RAGEngine.__new__(RAGEngine)
        default_fsm_context["pending_field"] = "email"
        
        result = await engine.route_fsm_turn(
            user_text="What are your features?",
            session_id="test_session",
            tenant_id="tara",
            language="english",
            fsm_context=default_fsm_context
        )
        
        assert result["action"] == "detour_rag"
        assert result["resume_prompt"] is not None
        assert "email" in result["resume_prompt"].lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
