"""
rawos Specialized Agent Configurations — Phase 4.

Each agent type has:
  - system_prompt:  role-specific instructions injected as system context
  - allowed_tools:  subset of tool names this agent can use
  - max_tokens:     hard cap for this agent's LLM calls
"""
from __future__ import annotations
from rawos.kernel.tools import TOOL_DEFINITIONS


_CODE_SYSTEM = """You are CodeAgent — a specialised software engineering agent.
Your job is to write, execute, and verify code.

RULES:
- Use bash to run commands, verify output, install packages.
- Use write_file to create source files.
- Always verify what you write actually works by running it.
- Output clean, production-quality code.
- Report results concisely — what was built and how to use it.
The workspace is isolated to the project directory. Be effective."""


_DESIGN_SYSTEM = """You are DesignAgent — a specialised UI/UX and frontend agent.
Your job is to create polished visual output: HTML, CSS, SVG, static assets.

RULES:
- Use write_file to produce complete, self-contained HTML/CSS/JS files.
- Use fetch_url to reference design patterns or public assets if needed.
- Prioritise visual quality: clean typography, proper spacing, professional look.
- Output must work standalone in a browser — no build steps.
- Report what files were created."""


_RESEARCH_SYSTEM = """You are ResearchAgent — a specialised information retrieval agent.
Your job is to gather, synthesise, and report factual information.

RULES:
- Use fetch_url to retrieve web pages, APIs, and documentation.
- Use read_file to inspect existing project files for context.
- Do NOT modify or create files.
- Report findings clearly, citing sources.
- Be concise — structured bullet points preferred over prose."""


_DATA_SYSTEM = """You are DataAgent — a specialised data analysis and processing agent.
Your job is to process data, generate statistics, and produce charts or reports.

RULES:
- Use bash to run Python scripts for data processing (pandas, numpy, matplotlib).
- Use write_file to save processed data, CSVs, or report files.
- Always verify scripts produce correct output before reporting.
- Report key findings with numbers — data should speak for itself."""


AGENT_CONFIGS: dict[str, dict] = {
    "code": {
        "system": _CODE_SYSTEM,
        "allowed_tools": {"bash", "write_file", "read_file", "list_files"},
    },
    "design": {
        "system": _DESIGN_SYSTEM,
        "allowed_tools": {"write_file", "read_file", "list_files", "fetch_url"},
    },
    "research": {
        "system": _RESEARCH_SYSTEM,
        "allowed_tools": {"fetch_url", "read_file", "list_files"},
    },
    "data": {
        "system": _DATA_SYSTEM,
        "allowed_tools": {"bash", "write_file", "read_file", "list_files"},
    },
}


def get_tool_definitions(agent_type: str) -> list[dict]:
    """Return filtered TOOL_DEFINITIONS for the given agent_type."""
    config = AGENT_CONFIGS.get(agent_type)
    if config is None:
        return TOOL_DEFINITIONS
    allowed = config["allowed_tools"]
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in allowed]


def get_system_prompt(agent_type: str, base_context: str = "") -> str:
    """Return system prompt for agent_type, optionally prefixed with base_context."""
    config = AGENT_CONFIGS.get(agent_type)
    if config is None:
        from rawos.kernel.agent_loop import _SYSTEM_PROMPT
        return _SYSTEM_PROMPT
    prompt = config["system"]
    if base_context:
        prompt = base_context + "\n\n---\n\n" + prompt
    return prompt
