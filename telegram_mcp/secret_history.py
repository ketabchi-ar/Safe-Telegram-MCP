"""What was said in a secret chat, kept across a restart, in both directions.

The package underneath holds an in-memory list of what ARRIVED, which is the right
scope for a library: a secret-chat implementation that silently wrote decrypted
plaintext to disk would be writing it somewhere the operator never chose, and the
package refuses to do that for key material for exactly the same reason.

But this server published `read_secret_messages` for the whole life of the previous
backend, and that backend kept a durable local database of BOTH directions. Two things
would therefore be lost by leaning on the package's list alone, and neither is
acceptable under "no capability lost":

* **outgoing messages**, which the package never records because nothing arrives for
  them - a conversation would read as one side talking;
* **anything at all after a restart**, because the list dies with the process.

So the decision to keep history is made HERE, by the application, and the file lands
under the server's own state directory beside the key store the operator already
protects. That is the difference that makes it acceptable: a library choosing to
persist plaintext chooses for everyone, an application choosing it chooses for itself.

**This file holds decrypted message text.** It is written owner-readable, it is never
logged, and `clear_secret_history` and `delete_secret_message` remove from it - a
delete that left the text here would be a delete in name only.
"""

from copy import deepcopy
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from telegram_mcp.settings import state_dir

__all__ = ["clear", "forget", "read", "record", "record_received"]

# kind -> the published `type`, which is the previous backend's spelling. Kept
# because `type` is a field callers switch on, and renaming it would be a silent
# break dressed as a tidy-up.
_TYPES = {
    "photo": "messagePhoto",
    "video": "messageVideo",
    "document": "messageDocument",
    "audio": "messageAudio",
    "animation": "messageAnimation",
    "sticker": "messageSticker",
    "video_note": "messageVideoNote",
    "voice_note": "messageVoiceNote",
}

#: How much of one chat is kept. A secret chat's history is a liability as well as a
#: convenience, and an unbounded plaintext file is the wrong default for one.
_PER_CHAT_LIMIT = 500

_cache: Dict[str, dict] = {}

# Protect the load/copy/persist/publish transaction from other local threads.
# This is process-local serialization, not a cross-process storage lease.
_history_lock = threading.RLock()


def _path(account: str) -> Path:
    return state_dir() / "secret-chats" / f"{account}-history.json"


def _load(account: str) -> dict:
    if account in _cache:
        return _cache[account]
    path = _path(account)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        state = {}
    else:
        # A failed read is not an empty history. Do not cache a substitute that
        # the next write would persist over the operator's recoverable bytes.
        try:
            state = json.loads(text)
        except ValueError:
            raise ValueError(
                "Secret-chat history is invalid; the existing file was preserved."
            ) from None
        if not isinstance(state, dict) or any(
            not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages)
            for messages in state.values()
        ):
            raise ValueError(
                "Secret-chat history has an invalid shape; the existing file was preserved."
            )
    _cache[account] = state
    return state


def _flush(account: str, state: dict) -> None:
    """Persist a private candidate, without publishing it to the live cache."""
    path = _path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        _restrict(Path(temporary))
        os.replace(temporary, path)
    except BaseException:
        # Cleanup must not hide the original storage failure.
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _commit(account: str, state: dict) -> None:
    """Publish only after replacement succeeds, while the caller holds the lock."""
    _flush(account, state)
    _cache[account] = state


def _restrict(path: Path) -> None:
    """Owner-only, best effort. This file holds plaintext somebody chose to encrypt."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def media_kind(media) -> Optional[str]:
    """The eight-kind name for a received media object, or ``None`` for text.

    The vocabulary is shared with the package and with `secret_media_content` by
    design - ADR 0003 made it backend-neutral precisely so a backend swap would not
    require a translation table here.
    """
    if media is None:
        return None
    name = type(media).__name__
    if "Photo" in name:
        return "photo"
    if "Document" not in name:
        # geoPoint, contact, venue, webPage, externalDocument: the protocol carries
        # them, they are not among the eight this server sends, and calling one a
        # `document` would misfile a location as a file. The class name itself is
        # the honest answer, and `entry` publishes it verbatim.
        return name.replace("DecryptedMessageMedia", "").lower() or "document"

    attributes = [type(a).__name__ for a in (getattr(media, "attributes", None) or [])]
    if any("Audio" in a for a in attributes):
        voice = any(getattr(a, "voice", False) for a in (getattr(media, "attributes", None) or []))
        return "voice_note" if voice else "audio"
    if any("Video" in a for a in attributes):
        round_video = any(
            getattr(a, "round_message", False) for a in (getattr(media, "attributes", None) or [])
        )
        return "video_note" if round_video else "video"
    if any("Sticker" in a for a in attributes):
        return "sticker"
    if any("Animated" in a for a in attributes):
        return "animation"
    return "document"


def _published_type(kind: Optional[str]) -> str:
    """The previous backend's spelling for a kind, or a faithful one for the rest."""
    if not kind:
        return "messageText"
    if kind in _TYPES:
        return _TYPES[kind]
    return "message" + kind[:1].upper() + kind[1:]


def entry(
    *,
    message_id: int,
    is_outgoing: bool,
    text: str = "",
    kind: Optional[str] = None,
    ttl: int = 0,
    file_id: Optional[int] = None,
) -> dict:
    """One message in the published shape.

    `date` is when this device saw the message, not Telegram's timestamp. The
    encrypted layer's message carries no date of its own - only the envelope does,
    and the package does not surface it - so this is the honest value rather than a
    fabricated one, and it orders a conversation correctly either way.
    """
    record = {
        "message_id": int(message_id),
        "is_outgoing": bool(is_outgoing),
        "date": int(time.time()),
        "type": _published_type(kind),
        # Nothing in the encrypted layer carries Telegram's `can_be_saved` flag: it
        # is a policy field on an ORDINARY message. The previous backend reported
        # what Telegram sent it; here there is nothing to report, and the honest
        # answer for a chat whose whole premise is that the peer already holds the
        # plaintext is that saving was never technically restricted.
        "can_be_saved": True,
    }
    if kind is None:
        record["text"] = text
    elif text:
        record["caption"] = text
    if ttl:
        record["self_destructs_after_seconds"] = int(ttl)
    if file_id is not None:
        record["file_id"] = int(file_id)
    return record


def record(account: str, chat_id: int, item: dict) -> None:
    """Append one message to a chat's history and persist it."""
    with _history_lock:
        state = deepcopy(_load(account))
        messages = state.setdefault(str(int(chat_id)), [])
        messages.append(deepcopy(item))
        if len(messages) > _PER_CHAT_LIMIT:
            del messages[: len(messages) - _PER_CHAT_LIMIT]
        _commit(account, state)


def record_received(account: str, message) -> None:
    """Append one arrived message, translating the package's event.

    `file_id` is the message's own `random_id`: the encrypted layer has no file ids,
    and `save_secret_media` needs to find the message again to decrypt its file. One
    id for both is not a shortcut - it is the only handle the protocol offers.
    """
    kind = media_kind(getattr(message, "media", None))
    record(
        account,
        message.chat_id,
        entry(
            message_id=message.random_id,
            is_outgoing=False,
            text=getattr(message, "text", "") or "",
            kind=kind,
            ttl=getattr(message, "ttl", 0) or 0,
            file_id=message.random_id if kind else None,
        ),
    )


def read(account: str, chat_id: int, limit: int) -> List[dict]:
    """The most recent `limit` messages, oldest first."""
    with _history_lock:
        return deepcopy(_load(account).get(str(int(chat_id)), [])[-limit:])


def forget(account: str, chat_id: int, message_ids) -> int:
    """Drop named messages. Returns how many were actually held here."""
    wanted = {int(m) for m in message_ids}
    with _history_lock:
        state = deepcopy(_load(account))
        key = str(int(chat_id))
        kept = [m for m in state.get(key, []) if m["message_id"] not in wanted]
        removed = len(state.get(key, [])) - len(kept)
        state[key] = kept
        _commit(account, state)
        return removed


def clear(account: str, chat_id: int) -> int:
    """Drop a whole chat's history. Returns how many messages went."""
    with _history_lock:
        state = deepcopy(_load(account))
        removed = len(state.pop(str(int(chat_id)), []))
        _commit(account, state)
        return removed
