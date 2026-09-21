"""History is published only after the matching file replacement succeeds."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest

from telegram_mcp import secret_history as history


@pytest.fixture(autouse=True)
def isolated_history(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(history, "_cache", {})


def _item(message_id):
    return history.entry(message_id=message_id, is_outgoing=False, text=f"message {message_id}")


def _seed():
    history.record("acct", 7, _item(1))
    history.record("acct", 7, _item(2))
    history.record("acct", 8, _item(3))


def _mutate(operation):
    if operation == "record":
        return history.record("acct", 7, _item(4))
    if operation == "forget":
        return history.forget("acct", 7, [1])
    return history.clear("acct", 7)


@pytest.mark.parametrize("operation", ["record", "forget", "clear"])
@pytest.mark.parametrize("failure", ["create", "write", "replace"])
def test_failed_mutation_preserves_cache_file_and_later_success(monkeypatch, operation, failure):
    _seed()
    original = deepcopy(history._cache["acct"])
    path = history._path("acct")
    original_bytes = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("synthetic storage failure")

    with monkeypatch.context() as patch:
        if failure == "create":
            patch.setattr(history.tempfile, "mkstemp", fail)
        elif failure == "write":
            patch.setattr(history.json, "dump", fail)
        else:
            patch.setattr(history.os, "replace", fail)
        with pytest.raises(OSError, match="synthetic storage failure"):
            _mutate(operation)
        assert history._cache["acct"] == original
        assert history.read("acct", 7, 100) == original["7"]
        assert path.read_bytes() == original_bytes
        assert not list(path.parent.glob("*.tmp"))

    # A later successful operation must not commit a previously rejected mutation.
    history.record("acct", 8, _item(5))
    history._cache.clear()
    assert history.read("acct", 7, 100) == original["7"]
    assert [m["message_id"] for m in history.read("acct", 8, 100)] == [3, 5]


@pytest.mark.parametrize("operation", ["record", "forget", "clear"])
def test_sync_failure_is_not_reported_as_a_success(monkeypatch, operation):
    _seed()
    before = deepcopy(history._cache["acct"])
    before_bytes = history._path("acct").read_bytes()

    def fail(*args):
        raise OSError("synthetic flush failure")

    monkeypatch.setattr(history.os, "fsync", fail)
    with pytest.raises(OSError, match="synthetic flush failure"):
        _mutate(operation)
    assert history._cache["acct"] == before
    assert history._path("acct").read_bytes() == before_bytes
    assert not list(history._path("acct").parent.glob("*.tmp"))


def test_failed_append_does_not_evict_the_oldest_message(monkeypatch):
    _seed()
    monkeypatch.setattr(history, "_PER_CHAT_LIMIT", 2)

    def fail(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(history.os, "replace", fail)
    with pytest.raises(OSError):
        history.record("acct", 7, _item(4))
    assert [m["message_id"] for m in history.read("acct", 7, 10)] == [1, 2]


def test_record_does_not_keep_a_mutable_reference_to_the_callers_message():
    item = _item(1)
    item["details"] = {"caption": "original"}
    history.record("acct", 7, item)
    item["text"] = "caller edit"
    item["details"]["caption"] = "caller edit"
    actual = history.read("acct", 7, 10)[0]
    assert actual["text"] == "message 1"
    assert actual["details"]["caption"] == "original"


def test_read_returns_a_detached_snapshot():
    _seed()
    messages = history.read("acct", 7, 10)
    messages[0]["text"] = "reader edit"
    messages.clear()
    assert [m["text"] for m in history.read("acct", 7, 10)] == ["message 1", "message 2"]


@pytest.mark.parametrize("payload", ["{", "[]", "null", '{"7": {}}', '{"7": [null]}'])
def test_corrupt_history_is_preserved_instead_of_overwritten(payload):
    path = history._path("acct")
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        history.record("acct", 7, _item(1))
    assert path.read_text(encoding="utf-8") == payload
    assert "acct" not in history._cache


def test_unreadable_history_is_not_replaced_with_an_empty_one(monkeypatch):
    _seed()
    path = history._path("acct")
    old_bytes = path.read_bytes()
    history._cache.clear()
    real_read = type(path).read_text

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic permission denial")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", denied)
    with pytest.raises(PermissionError):
        history.record("acct", 7, _item(9))
    assert path.read_bytes() == old_bytes
    assert "acct" not in history._cache


def test_successful_mutations_survive_reload_and_preserve_other_chats():
    _seed()
    assert history.forget("acct", 7, [1, 1, 999]) == 1
    history._cache.clear()
    assert [m["message_id"] for m in history.read("acct", 7, 10)] == [2]
    assert history.clear("acct", 7) == 1
    history._cache.clear()
    assert history.read("acct", 7, 10) == []
    assert [m["message_id"] for m in history.read("acct", 8, 10)] == [3]


def test_concurrent_writers_do_not_lose_each_others_committed_messages():
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: history.record("acct", 7, _item(value)), range(60)))
    actual = history.read("acct", 7, 100)
    assert sorted(m["message_id"] for m in actual) == list(range(60))
    assert json.loads(history._path("acct").read_text(encoding="utf-8"))["7"] == actual
    history._cache.clear()
    assert history.read("acct", 7, 100) == actual
