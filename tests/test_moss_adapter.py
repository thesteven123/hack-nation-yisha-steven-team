from types import SimpleNamespace

import pytest

from scientesis.adapters.moss import (
    MossAdapter,
    MossConfigurationError,
    MossSettings,
)


class FakeDocumentInfo:
    def __init__(self, id, text, metadata):
        self.id = id
        self.text = text
        self.metadata = metadata


class FakeQueryOptions:
    def __init__(self, top_k, alpha):
        self.top_k = top_k
        self.alpha = alpha


class FakeMutationOptions:
    def __init__(self, upsert):
        self.upsert = upsert


class FakeClient:
    def __init__(self, indexes=None, results=None):
        self.indexes = list(indexes or [])
        self.results = SimpleNamespace(docs=list(results or []))
        self.documents = []
        self.created = []
        self.added = []
        self.loaded = []
        self.queried = []
        self.deleted = []

    async def list_indexes(self):
        return self.indexes

    async def create_index(self, index_name, documents, model_id):
        self.created.append((index_name, documents, model_id))
        self.indexes.append(SimpleNamespace(name=index_name))
        return SimpleNamespace(job_id="job-create", doc_count=len(documents))

    async def add_docs(self, index_name, documents, options):
        self.added.append((index_name, documents, options))
        return SimpleNamespace(job_id="job-upsert", doc_count=len(documents))

    async def get_docs(self, index_name):
        return self.documents

    async def load_index(self, index_name):
        self.loaded.append(index_name)

    async def query(self, index_name, query, options):
        self.queried.append((index_name, query, options))
        return self.results

    async def delete_docs(self, index_name, ids):
        self.deleted.append((index_name, ids))


class FakeSDK:
    DocumentInfo = FakeDocumentInfo
    QueryOptions = FakeQueryOptions
    MutationOptions = FakeMutationOptions


def settings(index_name="scientesis-research"):
    return MossSettings("project-123", "test-secret", index_name)


def documents():
    return [
        {
            "id": "sc-project-brief",
            "text": "The active project question.",
            "metadata": {"project_id": "robust-robot-learning", "record_type": "research_brief"},
        }
    ]


def test_sync_creates_index_and_keeps_stable_document_ids():
    client = FakeClient()
    adapter = MossAdapter(settings(), client=client, sdk=FakeSDK)

    result = adapter.sync_documents(documents())

    assert result["index_name"] == "scientesis-research"
    assert result["indexed_count"] == 1
    assert result["document_ids"] == ["sc-project-brief"]
    assert client.created[0][0] == "scientesis-research"
    assert client.created[0][2] == "moss-minilm"
    assert client.deleted == []


def test_sync_upserts_and_removes_only_stale_documents_for_the_current_project():
    client = FakeClient(indexes=[SimpleNamespace(name="scientesis-research")])
    client.documents = [
        SimpleNamespace(id="stale-project-doc", metadata={"project_id": "robust-robot-learning"}),
        SimpleNamespace(id="other-project-doc", metadata={"project_id": "another-project"}),
        SimpleNamespace(id="malformed-doc", metadata=["not a metadata object"]),
        SimpleNamespace(id=42, metadata={"project_id": "robust-robot-learning"}),
    ]
    adapter = MossAdapter(settings(), client=client, sdk=FakeSDK)

    result = adapter.sync_documents(documents())

    assert result["indexed_count"] == 1
    assert result["removed_count"] == 1
    assert client.added[0][0] == "scientesis-research"
    assert client.added[0][2].upsert is True
    assert client.deleted == [("scientesis-research", ["stale-project-doc"])]


def test_search_loads_index_uses_query_options_and_filters_to_project():
    client = FakeClient(
        indexes=[SimpleNamespace(name="scientesis-research")],
        results=[
            SimpleNamespace(id="hit-1", text="Same-project evidence.", score=0.91, metadata={"project_id": "p1"}),
            SimpleNamespace(id="hit-2", text="Other-project record.", score=0.88, metadata={"project_id": "p2"}),
            SimpleNamespace(id="hit-3", text="Unscoped record.", score=0.70, metadata={}),
        ],
    )
    adapter = MossAdapter(settings(), client=client, sdk=FakeSDK)

    hits = adapter.search("noise robustness", top_k=4, alpha=0.65, project_id="p1")

    assert [hit["id"] for hit in hits] == ["hit-1"]
    assert client.loaded == ["scientesis-research"]
    _, query, options = client.queried[0]
    assert query == "noise robustness"
    assert options.top_k == 4
    assert options.alpha == 0.65


def test_settings_require_both_credentials_without_echoing_secret(monkeypatch):
    monkeypatch.delenv("MOSS_PROJECT_ID", raising=False)
    monkeypatch.delenv("MOSS_PROJECT_KEY", raising=False)

    with pytest.raises(MossConfigurationError, match="MOSS_PROJECT_ID.*MOSS_PROJECT_KEY"):
        MossSettings.from_environment()


def test_moss_sync_rejects_mixed_projects_and_invalid_query_options():
    mixed = documents() + [
        {"id": "other", "text": "Other project", "metadata": {"project_id": "different-project"}}
    ]
    adapter = MossAdapter(settings(), client=FakeClient(), sdk=FakeSDK)

    with pytest.raises(ValueError, match="exactly one project"):
        adapter.sync_documents(mixed)
    with pytest.raises(ValueError, match="result count"):
        adapter.search("query", top_k=0)
    with pytest.raises(ValueError, match="balance"):
        adapter.search("query", alpha=1.1)
