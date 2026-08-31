"""Deterministic synthetic claim-candidate discovery for offline use.

This module contains a real (if deliberately simple) sentence-clustering
heuristic. It is not a lookup table and does not hard-code the pilot fixture:
it splits captured messages into the application's paragraph units, keeps
significant non-stopword tokens, treats words that appear in more than roughly
a third of the units as corpus-wide noise, and joins units that share a
distinctive token into connected components. Components become claim
candidates; a component that spans several paragraphs of one message (or
paragraphs across several messages) is therefore identified as one coherent
thought cluster.

Evidence quotations are always the exact source text and offsets come from the
application's own segmentation. Candidate titles and summaries are generated
AI-interpretation placeholders, never source truth.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from nemoir.adapters.channel_naming import sanitize_channel_slug
from nemoir.domain.errors import ProviderOutputValidationError
from nemoir.domain.models import (
    AutonomousSortRequest,
    AutonomousSortResponse,
    AutonomousSortResult,
    AutonomousTopic,
    ClaimCandidate,
    ClaimCandidateSet,
    ClaimDiscoveryRequest,
    ClaimDiscoveryResponse,
    CoverageEntry,
    PromptRequest,
    PromptResponse,
    ProviderReceipt,
    SourceFragment,
    SourceUnit,
)
from nemoir.domain.states import Actionability, CoverageClassification, TendrilType

_WORD_RE = re.compile(r"[a-zA-Z0-9'-]+")
_PREFIX_LEN = 8

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "then", "else", "for", "nor",
        "so", "yet", "of", "in", "on", "at", "to", "from", "by", "with", "about",
        "against", "between", "into", "through", "during", "before", "after",
        "above", "below", "up", "down", "out", "off", "over", "under", "again",
        "further", "once", "here", "there", "when", "where", "why", "how", "all",
        "any", "both", "each", "few", "more", "most", "other", "some", "such",
        "no", "not", "only", "own", "same", "than", "too", "very", "can",
        "could", "may", "might", "must", "shall", "should", "would", "is",
        "are", "was", "were", "be", "been", "being", "have", "has", "had",
        "having", "do", "does", "did", "doing", "this", "that", "these",
        "those", "i", "me", "my", "mine", "myself", "we", "our", "ours",
        "ourselves", "you", "your", "yours", "yourself", "yourselves", "he",
        "him", "his", "himself", "she", "her", "hers", "herself", "it", "its",
        "itself", "they", "them", "their", "theirs", "themselves", "what",
        "which", "who", "whom", "whose", "as", "also", "whether", "rather",
        "maybe", "just", "even", "because", "while", "since", "until",
    }
)


def _stem(word: str) -> str:
    lowered = word.lower()
    return lowered[:_PREFIX_LEN] if len(lowered) > _PREFIX_LEN else lowered


def _significant_words(text: str) -> list[tuple[str, str]]:
    """Return ``(surface, stem)`` for non-stopword tokens in order."""
    words: list[tuple[str, str]] = []
    for match in _WORD_RE.finditer(text):
        surface = match.group(0).lower()
        if surface in _STOPWORDS or len(surface) < 2:
            continue
        words.append((surface, _stem(surface)))
    return words


def significant_stems(text: str) -> set[str]:
    """Return the set of significant stems in ``text`` (used for claim matching)."""
    return {stem for _surface, stem in _significant_words(text)}


_SMALL_TITLE_WORDS = frozenset(
    {"and", "of", "the", "for", "or", "in", "on", "a", "an", "to", "by", "with", "at"}
)


def _title_case(phrase: str) -> str:
    parts: list[str] = []
    for index, word in enumerate(phrase.split()):
        if word in _SMALL_TITLE_WORDS and index != 0:
            parts.append(word)
        elif word:
            parts.append(word[:1].upper() + word[1:])
        else:
            parts.append(word)
    return " ".join(parts)


def _first_bigram(word_lists: list[list[tuple[str, str]]]) -> str | None:
    for words in word_lists:
        for left, right in zip(words, words[1:]):
            return f"{left[0]} {right[0]}"
    return None


def _dominant_word(
    word_lists: list[list[tuple[str, str]]], lead_bigram: str | None
) -> str | None:
    counts: Counter[str] = Counter()
    first_index: dict[str, int] = {}
    position = 0
    for words in word_lists:
        for surface, _stem_value in words:
            if lead_bigram is not None and surface in lead_bigram.split():
                continue
            counts[surface] += 1
            if surface not in first_index:
                first_index[surface] = position
            position += 1
    if not counts:
        return None
    # Most frequent first; ties prefer earliest occurrence, then shortest word.
    return max(counts, key=lambda w: (counts[w], -first_index[w], -len(w)))


def _candidate_id(display_order: int) -> str:
    return f"cand-{display_order}"


def _topic_id(display_order: int) -> str:
    return f"topic-{display_order}"


def _sorted_clusters(
    units: list[SourceUnit],
    ordinal_by_message: dict[str, int],
) -> list[list[SourceUnit]]:
    """Deterministic content clustering: connected components of shared stems.

    Returns one list of unit clusters per component, ordered deterministically
    (largest component first, then earliest source position), with each
    component's units in source order. This is the shared clustering core for
    both claim-candidate discovery and the autonomous sort.
    """
    unit_by_id = {item.unit_id: item for item in units}
    words_by_unit: dict[str, list[tuple[str, str]]] = {
        unit.unit_id: _significant_words(unit.exact_text) for unit in units
    }

    stem_to_units: dict[str, set[str]] = defaultdict(set)
    for unit in units:
        for _surface, stem in words_by_unit[unit.unit_id]:
            stem_to_units[stem].add(unit.unit_id)

    # A stem is a clustering connector only when it is distinctive: it appears
    # in at least two units but not in a large share of the corpus.
    common_threshold = max(3, -(-len(units) // 3))
    connectors = {
        stem for stem, members in stem_to_units.items() if 2 <= len(members) < common_threshold
    }

    adjacency: dict[str, set[str]] = {unit.unit_id: set() for unit in units}
    for stem in connectors:
        members = sorted(stem_to_units[stem])
        for left, right in zip(members, members[1:]):
            adjacency[left].add(right)
            adjacency[right].add(left)

    def source_position(unit_id: str) -> tuple[int, int]:
        unit = unit_by_id[unit_id]
        return (ordinal_by_message.get(unit.source_message_id, 0), unit.paragraph_ordinal)

    seen: set[str] = set()
    clusters: list[list[SourceUnit]] = []
    for unit in units:
        if unit.unit_id in seen:
            continue
        stack = [unit.unit_id]
        component: list[SourceUnit] = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.append(unit_by_id[current])
            stack.extend(adjacency[current] - seen)
        component.sort(key=lambda item: source_position(item.unit_id))
        clusters.append(component)

    clusters.sort(
        key=lambda component: (-len(component), min(source_position(item.unit_id) for item in component))
    )
    return clusters


def synthetic_claim_candidates(request: ClaimDiscoveryRequest) -> ClaimCandidateSet:
    units = list(request.source_units)
    if not units:
        raise ValueError("Synthetic candidate discovery requires at least one source unit")
    ordinal_by_message = {item.external_message_id: item.ordinal for item in request.messages}
    unit_by_id = {item.unit_id: item for item in units}
    words_by_unit: dict[str, list[tuple[str, str]]] = {
        unit.unit_id: _significant_words(unit.exact_text) for unit in units
    }
    clusters = _sorted_clusters(units, ordinal_by_message)

    candidates: list[ClaimCandidate] = []
    for display_order, component in enumerate(clusters, start=1):
        evidence: list[SourceFragment] = []
        word_lists: list[list[tuple[str, str]]] = []
        for unit in component:
            evidence.append(
                SourceFragment(
                    source_message_id=unit.source_message_id,
                    exact_quote=unit.exact_text,
                    unit_ids=[unit.unit_id],
                    start_offset=unit.start_offset,
                    end_offset=unit.end_offset,
                )
            )
            word_lists.append(words_by_unit[unit.unit_id])

        lead_bigram = _first_bigram(word_lists)
        dominant = _dominant_word(word_lists, lead_bigram)
        topic = lead_bigram or dominant or "these statements"
        if lead_bigram and dominant and dominant not in topic.split():
            topic = f"{lead_bigram} and {dominant}"
        title = _title_case(topic)
        count = len(component)
        summary = f"{count} related statement{'s' if count != 1 else ''} about {topic}."
        candidates.append(
            ClaimCandidate(
                candidate_id=_candidate_id(display_order),
                title=title,
                summary=summary,
                evidence=evidence,
                display_order=display_order,
            )
        )

    if len(candidates) < 2:
        raise ValueError("Synthetic candidate discovery produced fewer than two candidates")
    return ClaimCandidateSet(bundle_id=request.bundle_id, candidates=candidates)


class SyntheticClaimDiscoveryProvider:
    """Deterministic claim-candidate provider; never performs live I/O."""

    def __init__(self, *, variant: str = "valid") -> None:
        self.variant = variant
        self.call_count = 0
        self.last_request: ClaimDiscoveryRequest | None = None

    async def discover_claim_candidates(
        self, request: ClaimDiscoveryRequest
    ) -> ClaimDiscoveryResponse:
        self.call_count += 1
        self.last_request = request
        result = synthetic_claim_candidates(request)
        if self.variant == "valid":
            pass
        elif self.variant == "invented_quote":
            candidate = result.candidates[0]
            mutated = candidate.model_copy(deep=True)
            mutated.evidence[0].exact_quote = "This quotation never existed."
            result = result.model_copy(
                update={"candidates": [mutated, *result.candidates[1:]]}
            )
        elif self.variant == "unknown_message":
            candidate = result.candidates[0]
            mutated = candidate.model_copy(deep=True)
            mutated.evidence[0].source_message_id = "missing-message"
            result = result.model_copy(
                update={"candidates": [mutated, *result.candidates[1:]]}
            )
        elif self.variant == "duplicate_id":
            result = result.model_copy(
                update={
                    "candidates": [
                        result.candidates[0].model_copy(
                            update={"candidate_id": result.candidates[1].candidate_id}
                        ),
                        *result.candidates[1:],
                    ]
                }
            )
        elif self.variant == "bad_order":
            first = result.candidates[0].model_copy(update={"display_order": 2})
            second = result.candidates[1].model_copy(update={"display_order": 2})
            result = result.model_copy(
                update={"candidates": [first, second, *result.candidates[2:]]}
            )
        elif self.variant == "empty_title":
            result = result.model_copy(
                update={
                    "candidates": [
                        result.candidates[0].model_copy(update={"title": "   "}),
                        *result.candidates[1:],
                    ]
                }
            )
        elif self.variant == "provider_failure":
            raise RuntimeError("Synthetic claim-discovery failure")
        else:
            raise ValueError(f"Unknown synthetic candidate variant: {self.variant}")

        return ClaimDiscoveryResponse(
            result=result,
            receipt=ProviderReceipt(
                provider="synthetic",
                model="deterministic-cluster",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                outcome="success",
                request_id=f"synthetic-{self.call_count}",
            ),
        )


_ACTIONABLE_MARKERS = re.compile(
    r"\b(should|must|need|needs|let's|lets|we need to|we must)\b", re.IGNORECASE
)


def synthetic_autonomous_sort(request: AutonomousSortRequest) -> AutonomousSortResult:
    """Deterministic recipient-free sort: every unit becomes exactly one topic.

    Each clustering component becomes one primary topic whose evidence quotes
    its units completely with the application's own offsets. Coverage assigns
    every sealed source unit to exactly one topic as a TENDRIL entry, so the
    strict ``validate_autonomous_sort`` contract passes without weakening any
    check. Topic IDs and display order are deterministic (``topic-1``..N in
    component order), never derived from wall-clock time or call history.
    """
    units = list(request.source_units)
    if not units:
        raise ValueError("Synthetic autonomous sort requires at least one source unit")
    ordinal_by_message = {item.external_message_id: item.ordinal for item in request.messages}
    clusters = _sorted_clusters(units, ordinal_by_message)

    topics: list[AutonomousTopic] = []
    coverage: list[CoverageEntry] = []
    for display_order, component in enumerate(clusters, start=1):
        evidence = [
            SourceFragment(
                source_message_id=unit.source_message_id,
                exact_quote=unit.exact_text,
                unit_ids=[unit.unit_id],
                start_offset=unit.start_offset,
                end_offset=unit.end_offset,
            )
            for unit in component
        ]
        word_lists = [_significant_words(unit.exact_text) for unit in component]
        lead_bigram = _first_bigram(word_lists)
        dominant = _dominant_word(word_lists, lead_bigram)
        topic = lead_bigram or dominant or "these statements"
        if lead_bigram and dominant and dominant not in topic.split():
            topic = f"{lead_bigram} and {dominant}"
        title = _title_case(topic)
        count = len(component)
        actionable = any(_ACTIONABLE_MARKERS.search(unit.exact_text) for unit in component)
        tendril_type = (
            TendrilType.ACTIONABLE_CANDIDATE if actionable else TendrilType.INTERESTING
        )
        actionability = (
            Actionability.CANDIDATE if actionable else Actionability.NOT_ACTIONABLE
        )
        client_id = _topic_id(display_order)
        try:
            slug = sanitize_channel_slug(title)
        except ValueError:
            slug = None
        topics.append(
            AutonomousTopic(
                provider_client_id=client_id,
                display_order=display_order,
                title=title,
                summary=f"{count} related statement{'s' if count != 1 else ''} about {topic}.",
                tendril_type=tendril_type,
                actionability=actionability,
                evidence=evidence,
                why_open=(
                    "Preserved as an open thought branch by the deterministic "
                    "synthetic autonomous sort; no recipient has claimed it."
                ),
                suggested_habitat_slug=slug,
                confidence=0.9,
            )
        )
        for unit in component:
            coverage.append(
                CoverageEntry(
                    source_message_id=unit.source_message_id,
                    unit_id=unit.unit_id,
                    classification=CoverageClassification.TENDRIL,
                    tendril_client_ids=[client_id],
                    reason="Primary topic assignment from the deterministic autonomous sort",
                    confidence=1.0,
                )
            )

    return AutonomousSortResult(
        bundle_id=request.bundle_id,
        topics=topics,
        coverage=coverage,
        unresolved=[],
    )


class SyntheticAutonomousSortProvider:
    """Deterministic autonomous-sort provider; never performs live I/O.

    It never reads DeepSeek configuration or makes any model request, so
    Autonomous Test mode can run with real Discord channel writes while DeepSeek
    stays completely untouched.
    """

    def __init__(self, *, variant: str = "valid") -> None:
        self.variant = variant
        self.call_count = 0
        self.last_request: AutonomousSortRequest | None = None

    async def sort(self, request: AutonomousSortRequest) -> AutonomousSortResponse:
        self.call_count += 1
        self.last_request = request
        result = synthetic_autonomous_sort(request)
        if self.variant == "valid":
            pass
        elif self.variant == "invented_quote":
            mutated = result.topics[0].model_copy(deep=True)
            mutated.evidence[0].exact_quote = "This quotation never existed."
            result = result.model_copy(
                update={"topics": [mutated, *result.topics[1:]]}
            )
        elif self.variant == "unknown_message":
            mutated = result.topics[0].model_copy(deep=True)
            mutated.evidence[0].source_message_id = "missing-message"
            result = result.model_copy(
                update={"topics": [mutated, *result.topics[1:]]}
            )
        elif self.variant == "dropped_coverage":
            result = result.model_copy(update={"coverage": result.coverage[:-1]})
        elif self.variant == "duplicated_evidence":
            second = result.topics[1].model_copy(deep=True)
            second.evidence = [*second.evidence, *result.topics[0].evidence]
            result = result.model_copy(
                update={"topics": [result.topics[0], second, *result.topics[2:]]}
            )
        elif self.variant == "context_coverage":
            first = result.coverage[0].model_copy(
                update={"classification": CoverageClassification.CONTEXT}
            )
            result = result.model_copy(
                update={"coverage": [first, *result.coverage[1:]]}
            )
        elif self.variant == "multi_target_coverage":
            if len(result.topics) > 1:
                first = result.coverage[0].model_copy(
                    update={
                        "tendril_client_ids": [
                            result.topics[0].provider_client_id,
                            result.topics[1].provider_client_id,
                        ]
                    }
                )
                result = result.model_copy(
                    update={"coverage": [first, *result.coverage[1:]]}
                )
        elif self.variant == "invalid_schema":
            raise ProviderOutputValidationError(
                receipt=ProviderReceipt(
                    provider="synthetic",
                    model="deterministic-autonomous-sort",
                    latency_ms=0,
                    input_tokens=0,
                    output_tokens=0,
                    outcome="invalid_response",
                    request_id=f"synthetic-invalid-{self.call_count}",
                ),
                raw_response='{"not": "an autonomous sort"}',
                validation_error_count=1,
            )
        elif self.variant == "provider_failure":
            raise RuntimeError("Synthetic autonomous-sort failure")
        else:
            raise ValueError(f"Unknown synthetic autonomous-sort variant: {self.variant}")

        return AutonomousSortResponse(
            result=result,
            receipt=ProviderReceipt(
                provider="synthetic-autonomous",
                model="deterministic-autonomous-sort",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                outcome="success",
                request_id=f"synthetic-autonomous-{self.call_count}",
            ),
        )


def synthetic_prompt_answer(question: str) -> str:
    """Deterministic, clearly synthetic single-turn answer for offline pilot use."""
    topic = question.strip()
    return (
        "Here is a synthetic Nemoir answer to your question:\n\n"
        f"**Question**\n> {topic}\n\n"
        "**Answer**\n"
        "This is a deterministic pilot response generated without any live model "
        "call. It is a placeholder that echoes the question so the pilot can "
        "exercise the single-turn prompt path end to end without contacting "
        "DeepSeek."
    )


class SyntheticPromptProvider:
    """Deterministic single-turn prompt provider; never performs live I/O."""

    def __init__(self, *, answer: str | None = None, variant: str = "valid") -> None:
        self.answer = answer
        self.variant = variant
        self.call_count = 0
        self.last_request: PromptRequest | None = None

    async def prompt(self, request: PromptRequest) -> PromptResponse:
        self.call_count += 1
        self.last_request = request
        if self.variant == "provider_failure":
            raise RuntimeError("Synthetic prompt failure")
        if self.variant != "valid":
            raise ValueError(f"Unknown synthetic prompt variant: {self.variant}")
        text = self.answer if self.answer is not None else synthetic_prompt_answer(request.question)
        return PromptResponse(
            answer=text,
            receipt=ProviderReceipt(
                provider="synthetic",
                model="deterministic-prompt",
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                outcome="success",
                request_id=f"synthetic-prompt-{self.call_count}",
            ),
        )
