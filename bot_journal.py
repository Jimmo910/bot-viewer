"""Write a bot-viewer journal from your own Telegram bot.

The Bot API does not let a bot read chat history, so the bot has to record what it sees and
what it sends. This module is standard library only; copy it into your bot or import it.

    import bot_journal

    JOURNAL = "/path/to/journal.jsonl"

    # an incoming update (Bot API "Message" object as a dict)
    bot_journal.log_message(JOURNAL, update["message"], "in", path="/downloads/photo.jpg")
    # the Message returned by sendMessage / sendPhoto / ...
    bot_journal.log_message(JOURNAL, result, "out")
    # an edited message (update["edited_message"]) is logged the same way; the viewer shows the latest text
    bot_journal.log_message(JOURNAL, update["edited_message"], "in")

python-telegram-bot: pass ``message.to_dict()``; aiogram 3: ``message.model_dump(mode="json", by_alias=True,
exclude_none=True)``. Or build the dict yourself and call ``append()`` — see the README for the format.
"""
import json
import os
import threading
import time
from datetime import datetime

__all__ = ["append", "entry_from_message", "log_message", "now"]

ATTACHMENT_KINDS = ("photo", "video", "video_note", "animation", "voice", "audio", "document", "sticker",
                    "location", "venue", "contact", "poll", "dice")
_lock = threading.Lock()


def now():
    """Local time in the journal format."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    try:                                        # ISO 8601 string (some libraries serialize dates that way)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt.astimezone() if dt.tzinfo else dt).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def append(path, entry):
    """Append one journal line. Safe to call from several threads; one write() per line."""
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with _lock, open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _user_name(user):
    if not isinstance(user, dict):
        return None
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return name or (("@" + user["username"]) if user.get("username") else None)


def _forward(msg):
    origin = msg.get("forward_origin")
    if isinstance(origin, dict):                # Bot API 7.0+
        return (_user_name(origin.get("sender_user")) or origin.get("sender_user_name")
                or (origin.get("sender_chat") or origin.get("chat") or {}).get("title"))
    return (_user_name(msg.get("forward_from")) or msg.get("forward_sender_name")
            or (msg.get("forward_from_chat") or {}).get("title"))


def _attachments(msg):
    out = []
    for kind in ATTACHMENT_KINDS:
        obj = msg.get(kind)
        if not obj or (kind == "location" and msg.get("venue")):
            continue
        if kind == "photo" and isinstance(obj, list):       # sizes; the last one is the largest
            obj = obj[-1]
        att = {"kind": kind}
        if isinstance(obj, dict):
            if obj.get("file_name"):
                att["name"] = obj["file_name"]
            if obj.get("file_size"):
                att["size"] = obj["file_size"]
            if kind == "sticker" and obj.get("emoji"):
                att["note"] = obj["emoji"]
            elif kind == "poll" and obj.get("question"):
                att["note"] = obj["question"]
            elif kind in ("location", "venue"):
                loc = obj.get("location", obj)
                if "latitude" in loc:
                    att["note"] = f"{loc['latitude']:.5f}, {loc['longitude']:.5f}"
        out.append(att)
    return out


def entry_from_message(msg, direction, path=None, via=None, **extra):
    """A journal entry for a Bot API Message dict.

    direction: "in" (from a user) or "out" (sent by the bot).
    path: local file of the message's media, if your bot downloaded it (absolute, or relative to the journal).
    via: free-form source tag; "auto" marks automatic replies (shown as a tag in the viewer).
    extra: any other fields, e.g. silent=True.
    """
    if direction not in ("in", "out"):
        raise ValueError("direction must be 'in' or 'out'")
    chat = msg.get("chat") or {}
    e = {"time": _time(msg.get("date")) or now(), "dir": direction, "chat_id": chat.get("id"),
         "user": _user_name(msg.get("from") or msg.get("from_user")) or chat.get("title"),
         "message_id": msg.get("message_id"), "text": msg.get("text") or msg.get("caption") or ""}
    reply = msg.get("reply_to_message")
    if isinstance(reply, dict):
        e["reply_to"] = reply.get("message_id")
        quote = (msg.get("quote") or {}).get("text") or reply.get("text") or reply.get("caption")
        if quote:
            e["reply_quote"] = quote[:300]
    fwd = _forward(msg)
    if fwd:
        e["forward"] = fwd
    atts = _attachments(msg)
    if path and atts:
        atts[0]["path"] = str(path)
    elif path:
        atts = [{"kind": "document", "path": str(path)}]
    if atts:
        e["attachments"] = atts
    if msg.get("edit_date"):
        e["edited"] = True
        e["edit_time"] = _time(msg["edit_date"])
    if via:
        e["via"] = via
    e.update(extra)
    return {k: v for k, v in e.items() if v not in (None, "")}


def log_message(journal, msg, direction, path=None, via=None, **extra):
    """entry_from_message() + append(); returns the entry."""
    e = entry_from_message(msg, direction, path=path, via=via, **extra)
    append(journal, e)
    return e
