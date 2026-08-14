"""Grouping episodes by *what the user asked*, not by which tools ran.

The gap this fills is the one that makes the whole promotion chain misfire on
real traffic.  Pattern discovery clustered episodes by the literal signature of
their tool sequence — the request text was never read, not even for keywords.
So "the user keeps asking the same kind of question" was never a thing the
system could detect.  Two consequences, both observed:

* the same intent asked five different ways produced five clusters of one, so
  nothing ever reached the support threshold;
* an intent handled *differently every time* — the strongest possible signal
  that a reusable capability is missing — was invisible, because varying tool
  sequences guarantee no two episodes share a signature.

Why not keyword matching: the questions that matter are paraphrases. "帮我看下
宁德时代的财务风险" and "分析一下 CATL 这家公司的偿债能力" are the same request
with almost no lexical overlap, and a keyword clusterer puts them in different
buckets — which is exactly the failure the current signature clustering already
has. So the primary path is embeddings, and the fallback is character-level
n-grams (which survive Chinese without a tokeniser) rather than word keywords.

The fallback is not a formality. Embeddings require a configured service; if one
is absent, silently producing zero clusters would look like "nothing recurs"
rather than "we cannot tell", and that distinction decides whether an operator
goes looking for a problem.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Cosine above which two requests count as the same intent.
#
# Calibrated against the configured embedding model (qwen3_embedding_8b, 4096d)
# on real Chinese requests rather than guessed:
#
#   0.987  同一请求换句话说            ← must cluster
#   0.756  同一意图不同措辞（偿债能力 / 财务风险）  ← must cluster
#   0.580  同领域不同意图              ← must not
#   0.40   完全无关                    ← must not
#
# So the boundary sits between 0.58 and 0.756, and the model's floor for
# unrelated text is ~0.4 rather than 0. An initial 0.86 was set by intuition and
# would have rejected the 0.756 pair — the exact paraphrase case this exists for.
EMBED_THRESHOLD = 0.70
# The lexical fallback is a floor, not a substitute. Measured on the same pairs:
#
#   0.409  同一请求换句话说
#   0.300  同一请求措辞小改
#   0.095  同一意图不同措辞   ← misses it almost entirely
#   0.000  无关
#
# It catches surface rewordings and is blind to genuine paraphrase. That is the
# ceiling of any lexical method, keyword matching included, and the reason the
# primary path has to be embeddings: without an embedding service the system can
# only detect that a user repeated themselves, not that they asked for the same
# thing in different words.
LEXICAL_THRESHOLD = 0.28
# Below this many characters a request carries too little signal to cluster on.
MIN_TEXT_LEN = 6

METHOD_EMBEDDING = "embedding"
METHOD_LEXICAL = "lexical_ngram"
METHOD_NONE = "unavailable"


@dataclass
class IntentCluster:
    """A group of episodes that asked for the same thing."""

    cluster_id: str
    method: str
    episode_ids: List[str] = field(default_factory=list)
    representative: str = ""
    # Distinct tool sequences observed for this one intent. High variety is the
    # signal: the same request handled a different way every time.
    tool_signatures: List[str] = field(default_factory=list)
    success_count: int = 0
    total_count: int = 0
    task_types: List[str] = field(default_factory=list)
    memory_refs: List[str] = field(default_factory=list)
    # Distinct conversations the intent appeared in. One chat asking the same
    # thing three times is one occasion, not three: a user rephrasing after a
    # bad answer is the single most common way to manufacture false recurrence.
    chat_ids: List[str] = field(default_factory=list)

    @property
    def support(self) -> int:
        return len(self.episode_ids)

    @property
    def occasions(self) -> int:
        """Separate conversations, which is what "keeps coming up" means."""
        return len(self.chat_ids) or len(self.episode_ids)

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_count if self.total_count else 0.0

    @property
    def plan_variety(self) -> int:
        return len(self.tool_signatures)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "method": self.method,
            "support": self.support,
            "occasions": self.occasions,
            "representative": self.representative[:120],
            "plan_variety": self.plan_variety,
            "success_rate": round(self.success_rate, 3),
            "episode_ids": self.episode_ids[:20],
            "task_types": list(self.task_types),
            "memory_refs": self.memory_refs[:40],
        }


# ── Entity normalisation ─────────────────────────────────────────────────────
#
# "CATL" and "宁德时代" are the same company, and an embedding model trained on
# general text does not reliably know that. Measured: the alias form failed to
# join its own cluster. Aliases are extremely common in Chinese business usage
# (English short name, stock ticker, abbreviation), so this is not a long-tail
# case — it is most of the traffic in a domain deployment.
#
# The alias table comes from the ontology packs the deployment already
# maintains, so this stays a *use* of curated domain knowledge rather than a
# second place where entity knowledge has to be kept correct.

_ALIAS_CACHE: Optional[Dict[str, str]] = None


def load_entity_aliases() -> Dict[str, str]:
    """``alias → canonical`` from the active ontology packs.

    Cached for the life of the process: this runs inside a nightly batch, and a
    pack published mid-batch changing how half the corpus clusters would make
    the run's output depend on its timing.
    """
    global _ALIAS_CACHE
    if _ALIAS_CACHE is not None:
        return _ALIAS_CACHE

    aliases: Dict[str, str] = {}
    try:
        from core.db.engine import SessionLocal
        from core.db.models import OntologyPack, OntologyPackVersion

        with SessionLocal() as db:
            version_ids = [
                pack.active_version_id
                for pack in db.query(OntologyPack)
                .filter(OntologyPack.active_version_id.isnot(None))
                .all()
            ]
            if version_ids:
                for version in (
                    db.query(OntologyPackVersion)
                    .filter(OntologyPackVersion.version_id.in_(version_ids))
                    .all()
                ):
                    content = getattr(version, "content", None) or {}
                    if not isinstance(content, dict):
                        continue
                    for concept in content.get("concepts") or []:
                        if not isinstance(concept, dict):
                            continue
                        canonical = str(concept.get("name") or "").strip()
                        if not canonical:
                            continue
                        for alias in concept.get("aliases") or []:
                            alias_text = str(alias).strip()
                            if alias_text and alias_text != canonical:
                                aliases[alias_text.lower()] = canonical
    except Exception as exc:  # noqa: BLE001
        logger.debug("[similarity] alias table unavailable: %s", exc)

    _ALIAS_CACHE = aliases
    return aliases


def reset_alias_cache() -> None:
    global _ALIAS_CACHE
    _ALIAS_CACHE = None


def canonicalise(text: str, aliases: Optional[Dict[str, str]] = None) -> str:
    """Rewrite known aliases to their canonical form.

    Longest-first so a short alias contained in a longer one does not fire
    first and leave the longer match unreachable.
    """
    table = load_entity_aliases() if aliases is None else aliases
    if not table:
        return text
    result = text
    for alias in sorted(table, key=len, reverse=True):
        if not alias:
            continue
        pattern = re.compile(re.escape(alias), re.IGNORECASE)
        result = pattern.sub(table[alias], result)
    return result


# ── Lexical fallback ─────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[\s，。？！、；：,.?!;:()（）\[\]【】\"'`~—\-_/\\|]+")


def _normalise(text: str) -> str:
    return _PUNCT.sub("", str(text or "")).lower()


def _ngrams(text: str, n: int = 3) -> set:
    """Character n-grams.

    Character-level on purpose: word segmentation for Chinese needs a tokeniser
    this module has no business depending on, and n-grams capture paraphrase
    overlap well enough to be a floor rather than a pretence.
    """
    cleaned = _normalise(text)
    if len(cleaned) < n:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + n] for i in range(len(cleaned) - n + 1)}


def lexical_similarity(a: str, b: str, n: int = 3) -> float:
    """Jaccard overlap of character n-grams.

    ``n`` defaults to 3 (unchanged for existing callers). Short surfaces — entity
    and concept names rather than whole questions — want ``n=2``: trigrams over a
    four-character name leave too few grams to overlap on.
    """
    ga, gb = _ngrams(a, n), _ngrams(b, n)
    if not ga or not gb:
        return 0.0
    intersection = len(ga & gb)
    union = len(ga | gb)
    return intersection / union if union else 0.0


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ── Embeddings ───────────────────────────────────────────────────────────────


def _embed_all(texts: Sequence[str]) -> Optional[List[List[float]]]:
    """Embed every request, or return ``None`` if the service is unavailable.

    All-or-nothing: a partially embedded batch would cluster some requests
    semantically and others not at all, and the resulting groups would be an
    artefact of which calls happened to succeed.
    """
    try:
        from core.kb.kb_vector import embed_batch

        vectors = embed_batch(list(texts))
    except Exception as exc:  # noqa: BLE001
        logger.info("[similarity] embedding unavailable, falling back to n-grams: %s", exc)
        return None
    if not vectors or len(vectors) != len(texts) or not all(vectors):
        logger.info("[similarity] embedding returned an incomplete batch; using n-grams")
        return None
    return vectors


# ── Clustering ───────────────────────────────────────────────────────────────


def _greedy_cluster(
    items: List[Tuple[str, str]],
    similarity,
    threshold: float,
) -> List[List[int]]:
    """Single-pass greedy clustering against cluster representatives.

    Not a full hierarchical clustering: this runs over a nightly batch of
    episodes and O(n²) pairwise comparison of a large corpus is the kind of cost
    that turns a background job into an incident. Comparing against one
    representative per cluster keeps it linear in clusters, and the failure mode
    of a greedy pass — occasionally splitting one intent into two — merely
    delays a candidate rather than fabricating one.
    """
    clusters: List[List[int]] = []
    reps: List[int] = []
    for index in range(len(items)):
        placed = False
        for slot, rep in enumerate(reps):
            if similarity(rep, index) >= threshold:
                clusters[slot].append(index)
                placed = True
                break
        if not placed:
            clusters.append([index])
            reps.append(index)
    return clusters


def cluster_intents(
    episodes: Sequence[Dict[str, Any]],
    *,
    min_support: int = 2,
    prefer_embeddings: bool = True,
) -> Tuple[List[IntentCluster], str]:
    """Group episodes by request intent.

    Returns ``(clusters, method)``. ``method`` is reported rather than hidden so
    a caller — and the console — can tell "nothing recurs" from "we had no way
    to look", which are very different operational states.
    """
    usable = [
        episode
        for episode in episodes
        if len(_normalise(episode.get("objective") or episode.get("objective_preview") or "")) >= MIN_TEXT_LEN
    ]
    if not usable:
        return [], METHOD_NONE

    aliases = load_entity_aliases()
    texts = [
        canonicalise(
            str(episode.get("objective") or episode.get("objective_preview") or ""), aliases
        )
        for episode in usable
    ]

    vectors = _embed_all(texts) if prefer_embeddings else None
    if vectors is not None:
        method = METHOD_EMBEDDING
        threshold = EMBED_THRESHOLD

        def similarity(i: int, j: int) -> float:
            return cosine(vectors[i], vectors[j])

    else:
        method = METHOD_LEXICAL
        threshold = LEXICAL_THRESHOLD

        def similarity(i: int, j: int) -> float:
            return lexical_similarity(texts[i], texts[j])

    groups = _greedy_cluster([(t, t) for t in texts], similarity, threshold)

    clusters: List[IntentCluster] = []
    for slot, members in enumerate(groups):
        if len(members) < min_support:
            continue
        signatures = set()
        task_types = set()
        memory_refs = set()
        chat_ids = set()
        success = 0
        for index in members:
            episode = usable[index]
            tools = [str(t) for t in (episode.get("tool_sequence") or [])]
            if tools:
                signatures.add("→".join(tools))
            if episode.get("verdict") == "success":
                success += 1
            task_type = str(episode.get("task_type") or "").strip()
            if task_type:
                task_types.add(task_type)
            for ref in episode.get("memory_refs") or []:
                if ref:
                    memory_refs.add(str(ref))
            chat_id = str(episode.get("chat_id") or "").strip()
            if chat_id:
                chat_ids.add(chat_id)
        clusters.append(
            IntentCluster(
                cluster_id=f"intent-{slot:03d}",
                method=method,
                episode_ids=[str(usable[i].get("episode_id") or "") for i in members],
                representative=texts[members[0]],
                tool_signatures=sorted(signatures),
                success_count=success,
                total_count=len(members),
                task_types=sorted(task_types),
                memory_refs=sorted(memory_refs),
                chat_ids=sorted(chat_ids),
            )
        )
    clusters.sort(key=lambda c: c.support, reverse=True)
    return clusters, method
