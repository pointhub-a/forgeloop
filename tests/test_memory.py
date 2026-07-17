import json
import sqlite3

import pytest

from forgeloop.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


def test_recall_is_project_and_tag_scoped(store):
    store.upsert("one", "style", "use ruff", ["python", "style"])
    store.upsert("two", "style", "use eslint", ["js", "style"])

    recalled = store.recall("one", ["python"], limit=5, char_budget=100)

    assert [memory.value for memory in recalled] == ["use ruff"]
    assert store.recall("one", ["js"], limit=5, char_budget=100) == []


def test_recall_respects_character_budget(store):
    store.upsert("p", "a", "12345", ["x"])
    store.upsert("p", "b", "67890", ["x"])

    recalled = store.recall("p", ["x"], 10, 5)

    assert sum(len(memory.value) for memory in recalled) <= 5


def test_memory_persists_between_store_instances(tmp_path):
    database = tmp_path / "memory.sqlite3"
    MemoryStore(database).upsert("project", "style", "use ruff", ["python"])

    recalled = MemoryStore(database).recall(
        "project", ["python"], limit=5, char_budget=100
    )

    assert [memory.value for memory in recalled] == ["use ruff"]


def test_memory_store_context_manager_closes_connection(tmp_path):
    database = tmp_path / "memory.sqlite3"

    with MemoryStore(database) as store:
        store.upsert("project", "style", "use ruff", ["python"])

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.recall("project", ["python"], limit=5, char_budget=100)


def test_upsert_replaces_project_key_and_normalizes_tags(tmp_path):
    database = tmp_path / "memory.sqlite3"
    store = MemoryStore(database)
    store.upsert("project", "style", "old", ["style"])
    store.upsert("project", "style", "new", ["python", "style", "python"])

    recalled = store.recall("project", ["python"], limit=5, char_budget=100)

    assert [memory.value for memory in recalled] == ["new"]
    with sqlite3.connect(database) as connection:
        encoded_tags, created_at, updated_at = connection.execute(
            """
            SELECT tags_json, created_at, updated_at
            FROM memories
            WHERE project_id = ? AND key = ?
            """,
            ("project", "style"),
        ).fetchone()
    assert json.loads(encoded_tags) == ["python", "style"]
    assert created_at <= updated_at


def test_recall_orders_by_tag_overlap_then_updated_at(store):
    store.upsert("project", "older", "older match", ["python"])
    store.upsert("project", "newer", "newer match", ["python"])
    store.upsert("project", "strongest", "strongest match", ["python", "style"])

    recalled = store.recall(
        "project", ["python", "style"], limit=5, char_budget=100
    )

    assert [memory.key for memory in recalled] == ["strongest", "newer", "older"]


def test_recall_respects_limit(store):
    store.upsert("project", "one", "1", ["python"])
    store.upsert("project", "two", "2", ["python"])

    recalled = store.recall("project", ["python"], limit=1, char_budget=100)

    assert len(recalled) == 1


@pytest.mark.parametrize(
    "key",
    [
        "secret",
        "access-token",
        "db_password",
        "openai_api_key",
        "openai-api-key",
    ],
)
def test_upsert_rejects_credential_shaped_keys(store, key):
    with pytest.raises(ValueError, match="sensitive"):
        store.upsert("project", key, "must not be stored", ["python"])


def test_upsert_rejects_token_shaped_value_without_persisting(tmp_path):
    database = tmp_path / "memory.sqlite3"
    store = MemoryStore(database)
    fake_token = "sk-unmistakably-fake-memory"

    with pytest.raises(ValueError, match="sensitive"):
        store.upsert("project", "provider note", fake_token, ["python"])

    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT COUNT(*) FROM memories WHERE value = ?", (fake_token,)
        ).fetchone()[0]
    assert persisted == 0
