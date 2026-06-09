"""
rawos Filesystem Manifester.

Writes proactive agent results directly into the user's project workspace.
Every file is tagged with HTML comment provenance so `rawos why <file>` works.

Naming: RAWOS_{domain}_{slug}_{ts}.md
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path

import rawos.db as db
from rawos.models import Artifact, ArtifactType

log = logging.getLogger("rawos.manifester.writer")

_MAX_CONTENT_BYTES = 32_768   # 32KB cap per proactive artifact


def _goal_slug(goal: str) -> str:
    """'debug jwt refresh token expiry' → 'debug_jwt_refresh_token'"""
    words = re.sub(r"[^a-z0-9 ]", "", goal.lower()).split()[:4]
    return "_".join(words) if words else "analysis"



def _clean_dsml(text: str) -> str:
    """
    Strip DeepSeek DSML tool-call markup from agent output.
    DSML delimiters use U+FF5C FULLWIDTH VERTICAL LINE (｜), not regular pipe.
    Removes everything between <｜｜DSML｜｜tool_calls> ... </｜｜DSML｜｜tool_calls>.
    Also strips any orphaned single-line DSML tags.
    """
    # Remove multi-line tool_calls blocks
    p = r'<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>'
    cleaned = re.sub(p, '', text, flags=re.DOTALL)
    # Remove orphaned single-line DSML tags (any remaining <｜｜DSML｜｜...> tags)
    cleaned = re.sub(r'<｜｜DSML｜｜[^>]*>', '', cleaned)
    cleaned = re.sub(r'</｜｜DSML｜｜[^>]*>', '', cleaned)
    # Collapse excessive blank lines
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


async def manifest_agent_result(
    user_id: str,
    project_id: str,
    workdir: str,
    goal: str,
    domain: str,
    content: str,
) -> tuple[str, str | None]:
    """
    Write result to {workdir}/RAWOS_{domain}_{slug}_{ts}.md.
    Returns (absolute_file_path, artifact_id).
    """
    slug = _goal_slug(goal)
    ts = int(time.time())
    domain_safe = re.sub(r"[^a-z0-9_-]", "-", domain.lower()).strip("-")
    filename = f"RAWOS_{domain_safe}_{slug}_{ts}.md"
    file_path = Path(workdir) / filename

    header = (
        f"<!-- rawos proactive analysis -->\n"
        f"<!-- goal: {goal} -->\n"
        f"<!-- domain: {domain} -->\n"
        f"<!-- generated: {ts} -->\n\n"
    )
    full_content = header + _clean_dsml(content)[:_MAX_CONTENT_BYTES]

    file_path.write_text(full_content, encoding="utf-8")

    # Store in artifacts table via proper model
    artifact = Artifact(
        id=str(uuid.uuid4()),
        user_id=user_id,
        project_id=project_id,
        type=ArtifactType.DOCUMENT,
        name=filename,
        path=str(file_path),
        content=full_content if len(full_content) <= 8192 else None,
        mime_type="text/markdown",
        size_bytes=len(full_content.encode()),
    )
    try:
        db.save_artifact(artifact)
    except Exception:
        log.exception("manifester: failed to save artifact record for %s", filename)

    log.info("manifested: %s (%d bytes)", file_path, len(full_content))
    return str(file_path), artifact.id


def get_provenance(file_path: str) -> dict | None:
    """Parse provenance from RAWOS_ file header comments."""
    p = Path(file_path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    result = {}
    for line in text.splitlines()[:6]:
        if m := re.match(r"<!-- goal: (.+) -->", line):
            result["goal"] = m.group(1)
        elif m := re.match(r"<!-- domain: (.+) -->", line):
            result["domain"] = m.group(1)
        elif m := re.match(r"<!-- generated: (\d+) -->", line):
            result["generated_at"] = int(m.group(1))
    return result if result else None



def _strip_code_fences(text: str) -> str:
    """Remove markdown code block wrappers if LLM ignores no-fence instructions."""
    m = re.match(r'^```[a-z]*\n(.*?)\n?```$', text.strip(), re.DOTALL)
    if m:
        return m.group(1)
    return text


async def manifest_code_fix(
    user_id: str,
    project_id: str,
    workdir: str,
    goal: str,
    target_file: str,
    corrected_content: str,
) -> tuple[str, str | None]:
    """
    Write a code fix file to {workdir}/RAWOS_fix_{slug}_{ts}{ext}.
    Header encodes target path so rawos apply can find and patch it.
    Returns (absolute_file_path, artifact_id).
    """
    ext = Path(target_file).suffix or ".py"
    slug = _goal_slug(goal)
    ts = int(time.time())
    filename = f"RAWOS_fix_{slug}_{ts}{ext}"
    file_path = Path(workdir) / filename

    header = (
        f"# rawos:target={target_file}\n"
        f"# rawos:ts={ts}\n"
        f"# rawos:description={goal}\n"
        f"# rawos:type=code_fix\n\n"
    )
    full_content = header + corrected_content.strip()

    file_path.write_text(full_content, encoding="utf-8")

    mime = "text/x-python" if ext == ".py" else "text/plain"
    artifact = Artifact(
        id=str(uuid.uuid4()),
        user_id=user_id,
        project_id=project_id,
        type=ArtifactType.DOCUMENT,
        name=filename,
        path=str(file_path),
        content=full_content if len(full_content) <= 8192 else None,
        mime_type=mime,
        size_bytes=len(full_content.encode()),
    )
    try:
        db.save_artifact(artifact)
    except Exception:
        log.exception("manifester: failed to save artifact record for %s", filename)

    log.info("code fix manifested: %s (%d bytes)", file_path, len(full_content))
    return str(file_path), artifact.id


def list_proactive_artifacts(user_id: str, limit: int = 20) -> list[dict]:
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT goal, confidence, file_path, created_at
               FROM proactive_artifacts
               WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
