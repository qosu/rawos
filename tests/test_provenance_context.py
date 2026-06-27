"""tests/test_provenance_context.py — SHP.4 I-SEC5 provenance tagging.

Verifies that <project_memory> carries provenance="untrusted" and that
_SYSTEM_PROMPT contains a TRUST BOUNDARY block instructing the LLM to
treat project_memory content as data, not instructions.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


class TestProjectMemoryProvenance:
    """<project_memory> must carry structural provenance annotation (I-SEC5)."""

    def _build_with_memory(self):
        from rawos.kernel.context_builder import build_context
        with patch("rawos.kernel.context_builder.db") as mock_db, \
             patch("rawos.kernel.context_builder.memory_index") as mock_mi, \
             patch("rawos.kernel.context_builder.get_user_model", return_value=None):
            mock_db.get_project_memories.return_value = []
            mock_mi.search_memories.return_value = [
                ("prior agent output — could contain injection", {"role": "assistant"})
            ]
            mock_mi.search_files.return_value = []
            return build_context("u1", "p1", "query")

    def test_project_memory_carries_provenance_untrusted(self):
        """<project_memory> tag must carry provenance="untrusted" (I-SEC5).

        Without this attribute, the LLM has no structural signal that stored
        memory is potentially adversarial data, not a trusted instruction.
        """
        _, system_addition = self._build_with_memory()
        assert 'provenance="untrusted"' in system_addition, (
            "<project_memory> block missing provenance=untrusted attribute; "
            "stored injections not structurally separated from trusted context"
        )

    def test_project_memory_instructs_treat_as_data(self):
        """<project_memory> description must direct the LLM to treat content as data."""
        _, system_addition = self._build_with_memory()
        lowered = system_addition.lower()
        assert "data" in lowered or "not instruction" in lowered, (
            "project_memory block must inform LLM that content is DATA, not instructions"
        )

    def test_no_project_memory_block_when_no_results(self):
        """When semantic search returns nothing, <project_memory> must not appear."""
        from rawos.kernel.context_builder import build_context
        with patch("rawos.kernel.context_builder.db") as mock_db, \
             patch("rawos.kernel.context_builder.memory_index") as mock_mi, \
             patch("rawos.kernel.context_builder.get_user_model", return_value=None):
            mock_db.get_project_memories.return_value = []
            mock_mi.search_memories.return_value = []
            mock_mi.search_files.return_value = []
            _, system_addition = build_context("u1", "p1", "query")
        assert "<project_memory" not in system_addition


class TestSystemPromptTrustBoundary:
    """_SYSTEM_PROMPT must contain a trust boundary block (I-SEC5)."""

    def test_system_prompt_has_trust_boundary_block(self):
        """_SYSTEM_PROMPT must contain a TRUST BOUNDARY instruction block.

        Without this, the LLM has no authoritative instruction to treat
        <project_memory> content as untrusted data rather than commands.
        """
        from rawos.kernel.agent_loop import _SYSTEM_PROMPT
        assert "TRUST BOUNDARY" in _SYSTEM_PROMPT, (
            "_SYSTEM_PROMPT missing TRUST BOUNDARY block; "
            "LLM will not know to discard stored injection attempts"
        )

    def test_system_prompt_references_project_memory_tag(self):
        """System prompt must explicitly reference <project_memory> as data-only."""
        from rawos.kernel.agent_loop import _SYSTEM_PROMPT
        assert "project_memory" in _SYSTEM_PROMPT, (
            "_SYSTEM_PROMPT does not reference project_memory; "
            "LLM cannot distinguish memory blocks from trusted instructions"
        )
