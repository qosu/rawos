"""
File serving routes — secure access to project workspace files.
All paths validated within project workdir before serving.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import anima.db as db
from anima.api.deps import current_user
from anima.kernel.sandbox import PathTraversalError, validate_path
from anima.models import User

router = APIRouter()


class FileEntry(BaseModel):
    name:       str
    path:       str
    is_dir:     bool
    size_bytes: int
    mime_type:  str | None


@router.get("/projects/{project_id}/files")
async def list_project_files(
    project_id: str,
    user: User = Depends(current_user),
) -> list[FileEntry]:
    project = db.get_project(user.id, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    if not project.workdir or not Path(project.workdir).is_dir():
        return []

    workdir = Path(project.workdir).resolve()
    entries: list[FileEntry] = []
    try:
        for fpath in sorted(workdir.rglob("*"), key=lambda p: (not p.is_dir(), str(p))):
            rel = str(fpath.relative_to(workdir))
            mime, _ = mimetypes.guess_type(str(fpath))
            entries.append(FileEntry(
                name=fpath.name,
                path=rel,
                is_dir=fpath.is_dir(),
                size_bytes=fpath.stat().st_size if fpath.is_file() else 0,
                mime_type=mime,
            ))
    except OSError:
        pass
    return entries


@router.get("/projects/{project_id}/files/{file_path:path}")
async def serve_project_file(
    project_id: str,
    file_path:  str,
    user: User = Depends(current_user),
):
    project = db.get_project(user.id, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    if not project.workdir:
        raise HTTPException(status_code=404, detail="workdir not initialised")

    try:
        full_path = validate_path(file_path, project.workdir)
    except PathTraversalError:
        raise HTTPException(status_code=400, detail="invalid file path")

    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    mime, _ = mimetypes.guess_type(str(full_path))
    return FileResponse(
        path=str(full_path),
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/projects/{project_id}/artifacts")
async def list_project_artifacts(
    project_id: str,
    user: User = Depends(current_user),
):
    project = db.get_project(user.id, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    artifacts = db.get_project_artifacts(user.id, project_id)
    return [
        {
            "id":         a.id,
            "name":       a.name,
            "type":       a.type.value,
            "mime_type":  a.mime_type,
            "size_bytes": a.size_bytes,
            "created_at": a.created_at,
        }
        for a in artifacts
    ]


_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)


@router.get("/preview/{project_id}/{file_path:path}")
async def preview_project_file(project_id: str, file_path: str):
    """Public file preview — no auth required; project UUID is the access token."""
    if not _UUID_RE.match(project_id):
        raise HTTPException(status_code=400, detail="invalid project id")

    workdir_str = db.get_workdir_by_project_id(project_id)
    if not workdir_str or not Path(workdir_str).is_dir():
        raise HTTPException(status_code=404, detail="project not found")

    try:
        full_path = validate_path(file_path, workdir_str)
    except PathTraversalError:
        raise HTTPException(status_code=400, detail="invalid file path")

    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    mime, _ = mimetypes.guess_type(str(full_path))
    return FileResponse(
        path=str(full_path),
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": "no-cache"},
    )
