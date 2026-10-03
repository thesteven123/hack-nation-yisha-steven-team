from __future__ import annotations

import asyncio
import importlib
import math
import os
import re
from dataclasses import dataclass


class MossConfigurationError(RuntimeError):
    pass


class MossDependencyError(RuntimeError):
    pass


class MossOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MossSettings:
    project_id: str
    project_key: str
    index_name: str = "scientesis-research"

    @classmethod
    def from_environment(cls) -> "MossSettings":
        project_id = os.environ.get("MOSS_PROJECT_ID", "").strip()
        project_key = os.environ.get("MOSS_PROJECT_KEY", "")
        index_name = os.environ.get("MOSS_INDEX_NAME", "scientesis-research").strip()
        missing = [name for name, value in (("MOSS_PROJECT_ID", project_id), ("MOSS_PROJECT_KEY", project_key)) if not value]
        if missing:
            raise MossConfigurationError("Set " + " and ".join(missing) + " before using Moss retrieval.")
        _validate_index_name(index_name)
        return cls(project_id, project_key, index_name)


class MossAdapter:
    def __init__(self, settings: MossSettings | None = None, client=None, sdk=None):
        self._settings = settings
        self._client = client
        self._sdk = sdk

    def sync_documents(self, documents: list[dict]) -> dict:
        settings = self._settings or MossSettings.from_environment()
        _validate_index_name(settings.index_name)
        normalized = _validate_documents(documents)
        project_ids = {document["metadata"]["project_id"] for document in normalized}
        if len(project_ids) != 1:
            raise ValueError("A Moss sync must contain records from exactly one project.")
        project_id = next(iter(project_ids))
        client, sdk = self._resolve_sdk(settings)
        moss_documents = [
            sdk.DocumentInfo(id=document["id"], text=document["text"], metadata=document["metadata"])
            for document in normalized
        ]
        desired_ids = {document["id"] for document in normalized}

        async def sync():
            indexes = await client.list_indexes()
            index_exists = any(_index_name(index) == settings.index_name for index in indexes)
            if not index_exists:
                result = await client.create_index(settings.index_name, moss_documents, "moss-minilm")
                return {
                    "index_name": settings.index_name,
                    "indexed_count": len(moss_documents),
                    "removed_count": 0,
                    "document_ids": sorted(desired_ids),
                    "mutation": _mutation_summary(result),
                }

            result = await client.add_docs(
                settings.index_name,
                moss_documents,
                sdk.MutationOptions(upsert=True),
            )
            existing_documents = await client.get_docs(settings.index_name)
            stale_ids = []
            for document in existing_documents:
                document_id = getattr(document, "id", None)
                metadata = getattr(document, "metadata", {}) or {}
                if (
                    isinstance(document_id, str)
                    and document_id not in desired_ids
                    and isinstance(metadata, dict)
                    and metadata.get("project_id") == project_id
                ):
                    stale_ids.append(document_id)
            stale_ids.sort()
            if stale_ids:
                await client.delete_docs(settings.index_name, stale_ids)
            return {
                "index_name": settings.index_name,
                "indexed_count": len(moss_documents),
                "removed_count": len(stale_ids),
                "document_ids": sorted(desired_ids),
                "mutation": _mutation_summary(result),
            }

        return self._run(sync(), settings)

    def search(self, query: str, top_k: int = 5, alpha: float = 0.8, project_id: str | None = None) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2_000:
            raise ValueError("Search query must be nonempty text under 2,000 characters.")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 25:
            raise ValueError("Search result count must be an integer from 1 to 25.")
        if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not math.isfinite(alpha) or not 0 <= alpha <= 1:
            raise ValueError("Search balance must be between 0 and 1.")
        settings = self._settings or MossSettings.from_environment()
        _validate_index_name(settings.index_name)
        client, sdk = self._resolve_sdk(settings)

        async def search():
            await client.load_index(settings.index_name)
            results = await client.query(
                settings.index_name,
                query.strip(),
                sdk.QueryOptions(top_k=top_k, alpha=float(alpha)),
            )
            hits = []
            for item in results.docs:
                document_id = getattr(item, "id", None)
                text = getattr(item, "text", None)
                score = getattr(item, "score", None)
                metadata = getattr(item, "metadata", {}) or {}
                if not isinstance(document_id, str) or not isinstance(text, str):
                    continue
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                    continue
                if not isinstance(metadata, dict):
                    metadata = {}
                if project_id is not None and metadata.get("project_id") != project_id:
                    continue
                hits.append({"id": document_id, "text": text, "score": float(score), "metadata": metadata})
            return hits

        return self._run(search(), settings)

    def _resolve_sdk(self, settings: MossSettings):
        if not settings.project_id.strip() or not settings.project_key:
            raise MossConfigurationError("Set MOSS_PROJECT_ID and MOSS_PROJECT_KEY before using Moss retrieval.")
        if self._client is not None and self._sdk is not None:
            return self._client, self._sdk
        if self._client is not None or self._sdk is not None:
            raise MossConfigurationError("Moss client and SDK must be provided together.")
        try:
            sdk = importlib.import_module("moss")
        except ImportError as error:
            raise MossDependencyError("Install the optional retrieval extra with: python -m pip install -e '.[retrieval]'.") from error
        self._sdk = sdk
        self._client = sdk.MossClient(settings.project_id, settings.project_key)
        return self._client, self._sdk

    @staticmethod
    def _run(coroutine, settings: MossSettings):
        try:
            return asyncio.run(coroutine)
        except (MossConfigurationError, MossDependencyError, MossOperationError):
            raise
        except Exception as error:
            message = str(error)
            if settings.project_key:
                message = message.replace(settings.project_key, "[redacted]")
            raise MossOperationError(f"Moss request failed: {type(error).__name__}: {message[:600]}") from error


def _validate_documents(documents: list[dict]) -> list[dict]:
    if not isinstance(documents, list) or not documents:
        raise ValueError("Moss sync requires at least one approved research document.")
    if len(documents) > 5_000:
        raise ValueError("Moss sync is limited to 5,000 documents at a time.")
    normalized = []
    seen = set()
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("Each Moss document must be a record.")
        document_id = document.get("id")
        text = document.get("text")
        metadata = document.get("metadata")
        if not isinstance(document_id, str) or not document_id.strip() or len(document_id) > 256:
            raise ValueError("Moss document IDs must be nonempty and no longer than 256 characters.")
        if document_id in seen:
            raise ValueError("Moss document IDs must be unique within a sync.")
        if not isinstance(text, str) or not text.strip() or len(text) > 50_000:
            raise ValueError("Moss document text must be nonempty and no longer than 50,000 characters.")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("project_id"), str) or not metadata["project_id"].strip():
            raise ValueError("Every Moss document needs project-scoped metadata.")
        seen.add(document_id)
        normalized.append({"id": document_id, "text": text, "metadata": dict(metadata)})
    return normalized


def _validate_index_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip() or name in {".", ".."} or len(name.encode("utf-8")) > 256:
        raise MossConfigurationError("Moss index name must be 1–256 bytes and cannot be blank, '.' or '..'.")
    if "/" in name or any(ord(character) < 32 for character in name):
        raise MossConfigurationError("Moss index name cannot contain slashes or control characters.")


def _index_name(index) -> str | None:
    return index if isinstance(index, str) else getattr(index, "name", None)



def _mutation_summary(result) -> dict:
    if result is None:
        return {}
    return {
        field: getattr(result, field)
        for field in ("job_id", "doc_count")
        if getattr(result, field, None) is not None
    }
