"""Model selects evidence; host validates and composes memory, never arbitrary rewrites."""
import personal_agent.workspace.constants as _pa_constants
import personal_agent.workspace.prompting as _pa_prompting
import json
import hashlib
import re
import unicodedata
from datetime import datetime
from personal_agent.workspace import file_memory;
from personal_agent.workspace.dream_files import TARGETS

def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

def plan(payload, answer, run_id, at):
    if not isinstance(answer, dict): raise ValueError("Invalid Dreaming response")
    operations, reflections = answer.get("operations"), answer.get("reflections")
    if not isinstance(operations, list) or len(operations) > 24: raise ValueError("Invalid operations")
    if not isinstance(reflections, list) or len(reflections) > 8 or any(not isinstance(s, str) or len(s) > 1000 for s in reflections):
        raise ValueError("Invalid reflections")
    before = payload["memory"]
    after = dict(before)
    sources = {s["id"]: s for s in payload["sources"]}
    outcomes, details = [], []
    for op in operations:
        if not isinstance(op, dict): raise ValueError("Invalid operation")
        target, key, action = op.get("target"), op.get("key"), op.get("action")
        quote, source = op.get("quote"), sources.get(op.get("source_id"))
        if target not in TARGETS or not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_-]{1,60}", key):
            raise ValueError("Invalid memory destination or key")
        if action not in {"add", "supersede", "skip", "conflict"}: raise ValueError("Invalid action")
        if not source or not isinstance(quote, str) or not 1 <= len(quote) <= 1500: raise ValueError("Missing evidence")
        evidence = source["input"] if target == "USER.md" else source["input"] + "\n" + source["result"]
        if quote not in evidence or file_memory.redact(quote) != quote or "[REDACTED]" in quote:
            raise ValueError("Unverified or secret evidence")
        if "<!--" in quote or "-->" in quote: raise ValueError("Invalid evidence markup")
        start, end = "<!-- dream-key:" + key + " ", "<!-- /dream-key:" + key + " -->"
        pattern = re.compile(re.escape(start) + r"([^\n]+) -->\n(.*?)\n" + re.escape(end), re.S)
        old = pattern.search(after[target])
        details += ["### " + target + " / " + key, "Source: " + source["path"] + " / " + source["id"],
            "Candidate:", "> " + file_memory.redact(quote).replace("\n", "\n> ")]
        if old:
            details += ["Prior:", "> " + file_memory.redact(old[2]).replace("\n", "\n> ").replace("<!--", "&lt;!--")]
        if not source.get("user_authored") or quote not in source.get("memory_statements", []):
            outcomes.append("conflict (review-only: owner memory intent or complete statement unverified): " + target + " / " + key)
            continue
        if action in {"skip", "conflict"}:
            outcomes.append(action + ": " + target + " / " + key)
            continue
        if normalized(quote) in normalized(after[target]):
            outcomes.append("duplicate: " + target + " / " + key)
            continue
        # Supersession may change only a complete managed entry with exact prior evidence.
        if old:
            try: prior = json.loads(old[1])
            except (ValueError, TypeError): raise ValueError("Invalid managed memory") from None
            if prior.get("digest") != hashlib.sha256(old[2].encode()).hexdigest():
                outcomes.append("conflict (manually edited entry): " + target + " / " + key)
                continue
            newer = datetime.fromisoformat(source["at"]) > datetime.fromisoformat(prior["at"])
            if action != "supersede" or op.get("prior") != old[2] or not newer or not source.get("user_authored") or quote not in source["input"]:
                outcomes.append("conflict: " + target + " / " + key)
                continue
        elif action == "supersede":
            outcomes.append("conflict (prior changed): " + target + " / " + key)
            continue
        metadata = json.dumps({"at": source["at"], "source": source["id"], "digest": hashlib.sha256(quote.encode()).hexdigest()}, ensure_ascii=False)
        # Store exact quoted evidence, not model-generated replacement prose.
        entry = start + metadata + " -->\n" + quote + "\n" + end
        after[target] = (after[target][:old.start()] + entry + after[target][old.end():]) if old else after[target].rstrip() + "\n\n" + entry + "\n"
        outcomes.append(action + ": " + target + " / " + key)
    if any(len(text.encode()) > _pa_constants.EDIT_MAX_BYTES for text in after.values()): raise ValueError("Memory file full")
    lines = ["## " + at + " · " + run_id, "", "Light — source review", ""]
    lines += ["- " + s["path"] + " · " + s["id"] for s in payload["sources"]]
    lines += ["- Skipped " + s["path"] + " / " + s["message_id"] + ": " + s["reason"] for s in payload.get("skipped", [])]
    lines += ["", "REM — reflections (observations, not established facts)", ""]
    lines += ["> " + file_memory.redact(s).replace("\n", "\n> ").replace("<!--", "&lt;!--") for s in reflections]
    lines += ["", "Deep — applied updates / duplicates / unresolved conflicts", ""]
    lines += ["- " + outcome for outcome in outcomes] or ["- No durable updates."]
    lines += ["", *details]
    lines += ["- Recovery: " + note for note in payload.get("recovery", [])]
    lines += ["", "Outcome: completed; unresolved conflicts require review in the memory editor.", ""]
    return {"before": before, "after": after, "report": "\n".join(lines), "outcomes": outcomes}

async def complete(payload, provider_id=None):
    from dataclasses import replace
    from core.models.config import ModelConfigService
    from core.models.chat_models import build_model_for_mode
    from agentscope.message import Msg, TextBlock
    service = ModelConfigService.get_instance()
    cfg = (service.resolve_provider(provider_id) if provider_id else None) or service.resolve("main_agent")
    model = build_model_for_mode(replace(cfg, temperature=0, max_tokens=5000, timeout=60), mode="fast", stream=False)
    response = await model([
        Msg(name="system", role="system", content=[TextBlock(text=_pa_prompting.prompt_part("dreaming"))]),
        Msg(name="user", role="user", content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))]),
    ])
    text = "".join(block.text for block in response.content if isinstance(block, TextBlock)).strip()
    if text.startswith("```json") and text.endswith("```"): text = text[7:-3].strip()
    return json.loads(text)
