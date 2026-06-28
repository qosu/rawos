"""
anima Memory Index — ChromaDB-backed semantic search over project memories and files.

Single collection "anima_memories" with doc_type metadata for unified search.
All queries are scoped to project_id for multi-tenant isolation.

doc_type = "memory" : episodic/semantic memory entries
doc_type = "file"   : project file contents
"""
from __future__ import annotations

import logging
import threading

from anima.config import settings

log = logging.getLogger("anima.memory_index")

_lock   = threading.Lock()
_client = None
_col    = None

_COLLECTION = "anima_memories"


def _get_collection():
    """Lazy initialisation — safe to call multiple times, thread-safe."""
    global _client, _col
    if _col is not None:
        return _col

    with _lock:
        if _col is not None:
            return _col

        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        _client = chromadb.PersistentClient(path=settings.chroma_path)
        _col = _client.get_or_create_collection(
            name=_COLLECTION,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
        log.info("ChromaDB initialised at %s (collection: %s)", settings.chroma_path, _COLLECTION)
        return _col


def warmup() -> None:
    """Call on server startup to pre-load the embedding model."""
    try:
        _get_collection()
        log.info("semantic memory index warmed up")
    except Exception as e:
        log.error("warmup failed: %s", e)


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def upsert_memory(
    memory_id:  str,
    text:       str,
    project_id: str,
    user_id:    str,
    tier:       str,
    role:       str,
    created_at: int = 0,
) -> None:
    """Add or update a memory document. No-op if text is empty."""
    if not text or not text.strip():
        return
    col = _get_collection()
    try:
        col.upsert(
            documents=[text[:16_000]],   # cap at 16 KB for embedding
            ids=[memory_id],
            metadatas=[{
                "project_id": project_id,
                "user_id":    user_id,
                "doc_type":   "memory",
                "tier":       tier,
                "role":       role,
                "created_at": created_at,
            }],
        )
    except Exception as e:
        log.error("upsert_memory failed for %s: %s", memory_id, e)


def upsert_file(
    file_id:    str,
    content:    str,
    project_id: str,
    user_id:    str,
    file_path:  str,
    file_name:  str,
) -> None:
    """Add or update a file document. No-op if content is empty."""
    if not content or not content.strip():
        return
    col = _get_collection()
    doc_id = f"file:{project_id}:{file_path}"
    try:
        col.upsert(
            documents=[content[:16_000]],
            ids=[doc_id],
            metadatas=[{
                "project_id": project_id,
                "user_id":    user_id,
                "doc_type":   "file",
                "file_id":    file_id,
                "file_path":  file_path,
                "file_name":  file_name,
            }],
        )
    except Exception as e:
        log.error("upsert_file failed for %s: %s", file_path, e)


def delete_memory(memory_id: str) -> None:
    try:
        _get_collection().delete(ids=[memory_id])
    except Exception as e:
        log.warning("delete_memory failed for %s: %s", memory_id, e)


def delete_memories_batch(memory_ids: list[str]) -> None:
    if not memory_ids:
        return
    try:
        _get_collection().delete(ids=memory_ids)
    except Exception as e:
        log.warning("delete_memories_batch failed: %s", e)


def delete_project_docs(project_id: str) -> None:
    """Remove ALL ChromaDB docs for a project."""
    col = _get_collection()
    try:
        result = col.get(where={"project_id": project_id})
        ids = result.get("ids", [])
        if ids:
            col.delete(ids=ids)
            log.info("deleted %d docs for project %s", len(ids), project_id)
    except Exception as e:
        log.warning("delete_project_docs failed: %s", e)


# ---------------------------------------------------------------------------
# Search operations
# ---------------------------------------------------------------------------

def _safe_query(query: str, n_results: int, where: dict) -> list[tuple[str, dict]]:
    """
    Query collection, return list of (document, metadata).
    Safely handles empty collections and filter mismatches.
    """
    col = _get_collection()
    try:
        # Check count first to avoid n_results > doc_count error
        try:
            count_result = col.get(where=where)
            available = len(count_result.get("ids", []))
        except Exception:
            available = n_results

        if available == 0:
            return []

        actual_n = min(n_results, available)
        result = col.query(
            query_texts=[query],
            n_results=actual_n,
            where=where,
        )
        docs   = result.get("documents", [[]])[0]
        metas  = result.get("metadatas", [[]])[0]
        return list(zip(docs, metas))
    except Exception as e:
        log.debug("_safe_query failed: %s", e)
        return []


def search_memories(
    project_id: str,
    query:      str,
    n_results:  int = 5,
) -> list[tuple[str, dict]]:
    """Return top-n semantically relevant memory docs for this project."""
    return _safe_query(
        query,
        n_results,
        where={"$and": [
            {"project_id": {"$eq": project_id}},
            {"doc_type":   {"$eq": "memory"}},
        ]},
    )


def search_files(
    project_id: str,
    query:      str,
    n_results:  int = 3,
) -> list[tuple[str, dict]]:
    """Return top-n semantically relevant file contents for this project."""
    return _safe_query(
        query,
        n_results,
        where={"$and": [
            {"project_id": {"$eq": project_id}},
            {"doc_type":   {"$eq": "file"}},
        ]},
    )


def get_indexed_ids(project_id: str) -> set[str]:
    """Return IDs of all indexed documents for this project (memories only)."""
    col = _get_collection()
    try:
        result = col.get(where={"$and": [
            {"project_id": {"$eq": project_id}},
            {"doc_type":   {"$eq": "memory"}},
        ]})
        return set(result.get("ids", []))
    except Exception:
        return set()
