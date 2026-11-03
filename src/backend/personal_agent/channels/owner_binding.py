"""Which IM accounts speak for the owner: an allowed list the owner keeps in the web settings.

The bot runs as its owner, so only the owner's own accounts may start or change work. The
platforms do not tell us which IM account is the owner's, and nothing a message says can
change that: only the owner's web settings add or remove accounts. The owner picks an
account from the ones that recently messaged the bot privately, or types its ID.

- Empty list: a private message gets a short "not on the list" reply and goes nowhere else.
- Listed accounts' private and group messages reach the agent. Anyone else's private message
  is dropped; anyone else's group message is kept as context only.
- Every private sender who is not listed is noted (ID, name, time; never the text), so the
  owner can find their own account in the settings.

Accounts bound with a pairing code (before the list existed) count as listed until the owner
edits the list. Owners learned before pairing existed are not trusted.
"""
import time

ALLOWED = "channel_allowed"
RECENT = "channel_recent_senders"
# Earlier pairing-code bindings: {channel_id: {sender_id, sender_name, keys, bound_at}}.
OWNERS = "channel_owners"
RECENT_MAX = 10
NOT_LISTED = ("这个账号还不在机器人的白名单里。主人请在网页端个人助理的「接入频道」里，"
              "从「最近私聊过机器人的账号」中把自己的账号加入白名单。")


def sender_keys(msg):
    """The identities of msg's sender, most stable first, each scoped so it cannot collide.

    DingTalk: senderStaffId is the org-scoped userid (absent for org-external users and before
    the bot is published), senderId the encrypted ID, senderUnionId the developer-wide ID. The
    adapter reports all three in raw; a userid is only trusted together with its org."""
    raw = getattr(msg, "raw", None) or {}
    ids = raw.get("dingtalk_sender") if msg.channel_type == "dingtalk" else None
    if ids:
        keys = []
        if ids.get("union_id"): keys.append("union:" + ids["union_id"])
        if ids.get("staff_id") and ids.get("corp_id"): keys.append("staff:" + ids["corp_id"] + ":" + ids["staff_id"])
        if ids.get("sender_id"): keys.append("sid:" + ids["sender_id"])
        return keys
    sender = str(msg.sender_id or "")
    return ["id:" + sender] if sender else []


def typed_keys(channel_type, sender_id):
    """The keys of an ID the owner typed. A DingTalk userid needs its organisation, so a typed
    DingTalk ID is matched as the union or encrypted ID only."""
    sender_id = (sender_id or "").strip()
    if not sender_id: return []
    return ["union:" + sender_id, "sid:" + sender_id] if channel_type == "dingtalk" else ["id:" + sender_id]


def allowed(config, channel_id):
    """The listed accounts: [{sender_id, sender_name, keys, added_at}]."""
    lists = config.get(ALLOWED) or {}
    if channel_id in lists: return list(lists[channel_id])
    entry = (config.get(OWNERS) or {}).get(channel_id)
    if entry and entry.get("bound_at") and entry.get("keys"):
        return [{"sender_id": entry.get("sender_id") or "", "sender_name": entry.get("sender_name") or "",
                 "keys": list(entry["keys"]), "added_at": entry["bound_at"]}]
    return []


def listed(config, channel_id, keys):
    """The listed account with one of these sender keys, or None."""
    keys = set(keys)
    return next((entry for entry in allowed(config, channel_id) if keys & set(entry.get("keys") or [])), None)


def _save(config, channel_id, entries):
    config[ALLOWED] = {**(config.get(ALLOWED) or {}), channel_id: entries}
    owners = dict(config.get(OWNERS) or {})
    if owners.pop(channel_id, None) is not None: config[OWNERS] = owners


def _note(config, channel_id, msg):
    keys = sender_keys(msg)
    if not keys: return
    recent = [item for item in (config.get(RECENT) or {}).get(channel_id, []) if not set(item["keys"]) & set(keys)]
    item = {"sender_id": str(msg.sender_id or ""), "sender_name": msg.sender_name or "", "keys": keys, "last_at": time.time()}
    config[RECENT] = {**(config.get(RECENT) or {}), channel_id: [item, *recent][:RECENT_MAX]}


def admission(config, channel_id, msg):
    """What one inbound message may do. Changes config (a listed name, a noted sender).

    "owner" (reaches the agent), "context" (a group message kept as background),
    "unlisted" (a private message while nobody is listed: reply NOT_LISTED), or "drop"."""
    entry = listed(config, channel_id, sender_keys(msg))
    if entry:
        if msg.sender_name and entry.get("sender_name") != msg.sender_name:
            _save(config, channel_id, [{**item, "sender_name": msg.sender_name} if item["keys"] == entry["keys"] else item
                                       for item in allowed(config, channel_id)])
        return "owner"
    if msg.chat_type == "group": return "context"
    _note(config, channel_id, msg)
    return "drop" if allowed(config, channel_id) else "unlisted"


def add(config, channel_id, *, keys, sender_id, sender_name=""):
    """List one account; an account already listed keeps its entry."""
    if not keys: raise ValueError("Account ID is required")
    entries = allowed(config, channel_id)
    if not any(set(keys) & set(item.get("keys") or []) for item in entries):
        entries.append({"sender_id": sender_id, "sender_name": sender_name, "keys": list(keys), "added_at": time.time()})
    _save(config, channel_id, entries)
    recent = (config.get(RECENT) or {}).get(channel_id, [])
    config[RECENT] = {**(config.get(RECENT) or {}),
                      channel_id: [item for item in recent if not set(item["keys"]) & set(keys)]}


def add_recent(config, channel_id, key):
    """List the recent private sender identified by key (one of its keys)."""
    item = next((item for item in (config.get(RECENT) or {}).get(channel_id, []) if key in item["keys"]), None)
    if not item: raise LookupError("Account not found")
    add(config, channel_id, keys=item["keys"], sender_id=item["sender_id"], sender_name=item["sender_name"])


def remove(config, channel_id, key):
    """Remove the listed account identified by key (one of its keys)."""
    _save(config, channel_id, [item for item in allowed(config, channel_id) if key not in (item.get("keys") or [])])


def access(config, channel_id):
    """What settings show: the listed accounts, and recent private senders not listed."""
    config = config or {}
    def shown(item, when):
        return {"key": item["keys"][0], "sender_id": item.get("sender_id") or "",
                "sender_name": item.get("sender_name") or "", when: item.get(when)}
    return {"allowed": [shown(item, "added_at") for item in allowed(config, channel_id)],
            "recent": [shown(item, "last_at") for item in (config.get(RECENT) or {}).get(channel_id, [])]}
