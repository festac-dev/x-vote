"""Typed domain models shared by the acquisition, rules and ranking layers."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 timestamp and normalize it to UTC.

    Naive timestamps are rejected: an unspecified zone is an ambiguity we refuse
    to guess about. Date-only values are also rejected.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp requires an explicit timezone: {value!r}")
    return parsed.astimezone(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def stable_hash(*parts: Any, length: int = 24) -> str:
    payload = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or stable_hash(value, length=8)


class CollectionStatus(StrEnum):
    NOT_STARTED = "not_started"
    COLLECTING = "collecting"
    COMPLETE_FOR_SCOPE = "complete_for_scope"
    PARTIALLY_COMPLETE = "partially_complete"
    RATE_LIMITED = "rate_limited"
    PERMISSION_RESTRICTED = "permission_restricted"
    SOURCE_UNAVAILABLE = "source_unavailable"
    FAILED = "failed"
    MANUALLY_SUPPLIED = "manually_supplied"


class EligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    PENDING_ELIGIBILITY_REVIEW = "pending_eligibility_review"
    ELIGIBILITY_UNKNOWN = "eligibility_unknown"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    PENDING_REVIEW = "pending_review"
    DISPUTED = "disputed"
    NOT_INDIVIDUALLY_VERIFIABLE = "not_individually_verifiable"


class Confidence(StrEnum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_INDIVIDUALLY_VERIFIABLE = "not_individually_verifiable"


CONFIDENCE_ORDER = {
    Confidence.HIGH: 4,
    Confidence.MODERATE: 3,
    Confidence.LOW: 2,
    Confidence.INSUFFICIENT_EVIDENCE: 1,
    Confidence.NOT_INDIVIDUALLY_VERIFIABLE: 0,
}


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    IN_REVIEW = "in_review"
    REVIEWED = "reviewed"
    MANUAL_OVERRIDE = "manual_override"


class VotingMethod(StrEnum):
    NATIVE_POLL = "native_poll"
    REPLY = "reply"
    LIKE = "like"
    REPOST = "repost"
    QUOTE = "quote"
    MENTION = "mention"
    HASHTAG = "hashtag"
    KEYWORD = "keyword"
    MANUAL_TOTAL = "manual_total"
    EXTERNAL_AGGREGATE = "external_aggregate"
    OCR_TOTAL = "ocr_total"
    CUSTOM = "custom"


class SourceType(StrEnum):
    X_POST = "x_post"
    POLL = "poll"
    REPLIES = "replies"
    LIKES = "likes"
    REPOSTS = "reposts"
    QUOTES = "quotes"
    MENTIONS = "mentions"
    HASHTAGS = "hashtags"
    KEYWORDS = "keywords"
    MANUAL = "manual"
    CSV = "csv"
    JSON = "json"
    OCR = "ocr"
    FIXTURE = "fixture"
    EXTERNAL = "external"


AGGREGATE_METHODS = {
    VotingMethod.NATIVE_POLL.value,
    VotingMethod.MANUAL_TOTAL.value,
    VotingMethod.EXTERNAL_AGGREGATE.value,
    VotingMethod.OCR_TOTAL.value,
}

INDIVIDUAL_METHODS = {
    VotingMethod.REPLY.value,
    VotingMethod.LIKE.value,
    VotingMethod.REPOST.value,
    VotingMethod.QUOTE.value,
    VotingMethod.MENTION.value,
    VotingMethod.HASHTAG.value,
    VotingMethod.KEYWORD.value,
    VotingMethod.CUSTOM.value,
}


@dataclass
class Project:
    name: str
    project_id: str = field(default_factory=lambda: f"prj_{uuid.uuid4().hex[:12]}")
    aliases: list[str] = field(default_factory=list)
    external_id: str = ""
    usernames: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("project name is required")
        self.aliases = _clean_list(self.aliases)
        self.usernames = [u.lstrip("@").lower() for u in _clean_list(self.usernames)]
        self.hashtags = [h.lstrip("#").lower() for h in _clean_list(self.hashtags)]
        self.keywords = _clean_list(self.keywords)
        self.domains = [d.lower().strip(".") for d in _clean_list(self.domains)]
        self.external_id = (self.external_id or "").strip()

    def identifiers(self) -> list[str]:
        return [self.name, *self.aliases, *self.usernames, *self.hashtags, *self.keywords]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "external_id": self.external_id,
            "aliases": list(self.aliases),
            "usernames": list(self.usernames),
            "hashtags": list(self.hashtags),
            "keywords": list(self.keywords),
            "domains": list(self.domains),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Project:
        return cls(
            name=str(data.get("name", "")),
            project_id=str(data.get("project_id") or f"prj_{uuid.uuid4().hex[:12]}"),
            aliases=list(data.get("aliases") or []),
            external_id=str(data.get("external_id") or ""),
            usernames=list(data.get("usernames") or []),
            hashtags=list(data.get("hashtags") or []),
            keywords=list(data.get("keywords") or []),
            domains=list(data.get("domains") or []),
        )


def _clean_limits(values: dict[str, Any]) -> dict[str, int]:
    limits: dict[str, int] = {}
    for key, value in values.items():
        if key not in {item.value for item in VotingMethod}:
            continue
        try:
            limit = int(value)
        except (TypeError, ValueError):
            continue
        if limit > 0:
            limits[str(key)] = limit
    return limits


def _clean_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


@dataclass
class Source:
    source_type: str
    post_id: str = ""
    url: str = ""
    label: str = ""
    source_id: str = field(default_factory=lambda: f"src_{uuid.uuid4().hex[:10]}")
    methods: list[str] = field(default_factory=list)
    adapter_hint: str = ""
    fixture_ref: str = ""
    notes: str = ""
    project_id: str = ""

    def __post_init__(self) -> None:
        if self.source_type not in {item.value for item in SourceType}:
            raise ValueError(f"unsupported source type: {self.source_type}")
        self.methods = _clean_list(self.methods)
        for method in self.methods:
            if method not in {item.value for item in VotingMethod}:
                raise ValueError(f"unsupported voting method: {method}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "post_id": self.post_id,
            "url": self.url,
            "label": self.label,
            "methods": list(self.methods),
            "adapter_hint": self.adapter_hint,
            "fixture_ref": self.fixture_ref,
            "notes": self.notes,
            "project_id": self.project_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        return cls(
            source_type=str(data.get("source_type") or "x_post"),
            post_id=str(data.get("post_id") or ""),
            url=str(data.get("url") or ""),
            label=str(data.get("label") or ""),
            source_id=str(data.get("source_id") or f"src_{uuid.uuid4().hex[:10]}"),
            methods=list(data.get("methods") or []),
            adapter_hint=str(data.get("adapter_hint") or ""),
            fixture_ref=str(data.get("fixture_ref") or ""),
            notes=str(data.get("notes") or ""),
            project_id=str(data.get("project_id") or ""),
        )


@dataclass
class EligibilityRules:
    allowed_methods: list[str] = field(default_factory=list)
    required_source_ids: list[str] = field(default_factory=list)
    required_keywords: list[str] = field(default_factory=list)
    required_hashtags: list[str] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)
    first_or_last: str = "first"  # all | first | last
    one_vote_per_account: bool = True
    apply_per_method: bool = True
    allow_multiple_projects: bool = False
    conflicting_votes_require_review: bool = True
    edits_affect_eligibility: bool = True
    deleted_content_remains_eligible: bool = False
    manual_overrides: bool = True
    unresolved_provisional: bool = True
    strictness: str = "standard"  # lenient | standard | strict
    association_mode: str = "any_identifier"
    require_support_statement: bool = True
    per_method_limits: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EligibilityRules:
        data = data or {}
        allowed = _clean_list(data.get("allowed_methods") or [])
        for method in allowed:
            if method not in {item.value for item in VotingMethod}:
                raise ValueError(f"unsupported voting method: {method}")
        if data.get("first_or_last", "first") not in {"all", "first", "last"}:
            raise ValueError("first_or_last must be all, first or last")
        if data.get("strictness", "standard") not in {"lenient", "standard", "strict"}:
            raise ValueError("strictness must be lenient, standard or strict")
        return cls(
            allowed_methods=allowed,
            required_source_ids=_clean_list(data.get("required_source_ids") or []),
            required_keywords=_clean_list(data.get("required_keywords") or []),
            required_hashtags=[
                h.lstrip("#") for h in _clean_list(data.get("required_hashtags") or [])
            ],
            excluded_keywords=_clean_list(data.get("excluded_keywords") or []),
            first_or_last=str(data.get("first_or_last", "first")),
            one_vote_per_account=bool(data.get("one_vote_per_account", True)),
            apply_per_method=bool(data.get("apply_per_method", True)),
            allow_multiple_projects=bool(data.get("allow_multiple_projects", False)),
            conflicting_votes_require_review=bool(
                data.get("conflicting_votes_require_review", True)
            ),
            edits_affect_eligibility=bool(data.get("edits_affect_eligibility", True)),
            deleted_content_remains_eligible=bool(
                data.get("deleted_content_remains_eligible", False)
            ),
            manual_overrides=bool(data.get("manual_overrides", True)),
            unresolved_provisional=bool(data.get("unresolved_provisional", True)),
            strictness=str(data.get("strictness", "standard")),
            association_mode=str(data.get("association_mode", "any_identifier")),
            require_support_statement=bool(data.get("require_support_statement", True)),
            per_method_limits=_clean_limits(data.get("per_method_limits") or {}),
        )


@dataclass
class EvidenceRules:
    require_source_url: bool = True
    require_timestamp: bool = True
    require_identity_for_votes: bool = False
    manual_requires_review: bool = True
    minimum_confidence: str = "low"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EvidenceRules:
        data = data or {}
        return cls(
            require_source_url=bool(data.get("require_source_url", True)),
            require_timestamp=bool(data.get("require_timestamp", True)),
            require_identity_for_votes=bool(data.get("require_identity_for_votes", False)),
            manual_requires_review=bool(data.get("manual_requires_review", True)),
            minimum_confidence=str(data.get("minimum_confidence", "low")),
        )


@dataclass
class RankingRules:
    mode: str = "confirmed"  # confirmed | unique_account | weighted | custom | separate
    weights: dict[str, float] = field(default_factory=dict)
    custom_formula: str = ""
    combine_confirmed_with_aggregates: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "weights": dict(self.weights),
            "custom_formula": self.custom_formula,
            "combine_confirmed_with_aggregates": self.combine_confirmed_with_aggregates,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RankingRules:
        data = data or {}
        mode = str(data.get("mode", "confirmed"))
        if mode not in {"confirmed", "unique_account", "weighted", "custom", "separate"}:
            raise ValueError(
                "ranking mode must be confirmed, unique_account, weighted, custom or separate"
            )
        weights = data.get("weights") or {}
        if not isinstance(weights, dict):
            raise ValueError("weights must be an object")
        clean_weights: dict[str, float] = {}
        for key, value in weights.items():
            if key not in {item.value for item in VotingMethod}:
                raise ValueError(f"unsupported weighted method: {key}")
            try:
                clean_weights[str(key)] = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"weight for {key} must be numeric") from exc
        return cls(
            mode=mode,
            weights=clean_weights,
            custom_formula=str(data.get("custom_formula") or ""),
            combine_confirmed_with_aggregates=bool(
                data.get("combine_confirmed_with_aggregates", False)
            ),
        )


@dataclass
class CollectionSettings:
    frequency_seconds: int = 0
    permitted_duration_seconds: int = 0
    adapter_preference: list[str] = field(default_factory=list)
    storage_mode: str = "auto"  # auto | memory | sqlite
    snapshot_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CollectionSettings:
        data = data or {}
        mode = str(data.get("storage_mode", "auto"))
        if mode not in {"auto", "memory", "sqlite"}:
            raise ValueError("storage_mode must be auto, memory or sqlite")
        return cls(
            frequency_seconds=max(0, int(data.get("frequency_seconds", 0) or 0)),
            permitted_duration_seconds=max(0, int(data.get("permitted_duration_seconds", 0) or 0)),
            adapter_preference=_clean_list(data.get("adapter_preference") or []),
            storage_mode=mode,
            snapshot_enabled=bool(data.get("snapshot_enabled", True)),
        )


@dataclass
class Competition:
    name: str
    description: str = ""
    competition_id: str = field(default_factory=lambda: f"cmp_{uuid.uuid4().hex[:12]}")
    timezone_name: str = "UTC"
    starts_at: str = ""
    ends_at: str = ""
    projects: list[Project] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    eligibility: EligibilityRules = field(default_factory=EligibilityRules)
    evidence: EvidenceRules = field(default_factory=EvidenceRules)
    ranking: RankingRules = field(default_factory=RankingRules)
    collection: CollectionSettings = field(default_factory=CollectionSettings)
    status: str = "collecting"  # collecting | provisional | finalized
    ruleset_version: str = ""
    created_at: str = field(default_factory=lambda: isoformat(utc_now()))
    updated_at: str = field(default_factory=lambda: isoformat(utc_now()))

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("competition name is required")
        if self.status not in {"collecting", "provisional", "finalized"}:
            raise ValueError("competition status must be collecting, provisional or finalized")
        self.methods = _clean_list(self.methods) or [
            method for method in self.methods if method in {item.value for item in VotingMethod}
        ]
        for method in self.methods:
            if method not in {item.value for item in VotingMethod}:
                raise ValueError(f"unsupported voting method: {method}")
        self.eligibility.allowed_methods = self.eligibility.allowed_methods or list(self.methods)
        if not self.ruleset_version:
            self.ruleset_version = self.compute_ruleset_version()

    def ruleset_payload(self) -> dict[str, Any]:
        return {
            "competition_id": self.competition_id,
            "timezone": self.timezone_name,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "projects": [
                {
                    "project_id": p.project_id,
                    "name": p.name,
                    "external_id": p.external_id,
                    "aliases": p.aliases,
                    "usernames": p.usernames,
                    "hashtags": p.hashtags,
                    "keywords": p.keywords,
                    "domains": p.domains,
                }
                for p in self.projects
            ],
            "methods": self.methods,
            "eligibility": self.eligibility.to_dict(),
            "evidence": self.evidence.to_dict(),
            "ranking": self.ranking.to_dict(),
        }

    def compute_ruleset_version(self) -> str:
        return stable_hash(canonical_json(self.ruleset_payload()), length=20)

    def refresh_ruleset(self) -> str:
        self.ruleset_version = self.compute_ruleset_version()
        self.updated_at = isoformat(utc_now())
        return self.ruleset_version

    def project_by_id(self, project_id: str) -> Project | None:
        return next((p for p in self.projects if p.project_id == project_id), None)

    def source_by_id(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.source_id == source_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "competition_id": self.competition_id,
            "name": self.name,
            "description": self.description,
            "timezone_name": self.timezone_name,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "projects": [p.to_dict() for p in self.projects],
            "sources": [s.to_dict() for s in self.sources],
            "methods": list(self.methods),
            "eligibility": self.eligibility.to_dict(),
            "evidence": self.evidence.to_dict(),
            "ranking": self.ranking.to_dict(),
            "collection": self.collection.to_dict(),
            "status": self.status,
            "ruleset_version": self.ruleset_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Competition:
        projects = [Project.from_dict(p) for p in data.get("projects") or []]
        seen_names: set[str] = set()
        for project in projects:
            key = project.name.casefold()
            if key in seen_names:
                raise ValueError(f"duplicate project name: {project.name}")
            seen_names.add(key)
        project_ids = {project.name.casefold(): project.project_id for project in projects}
        sources: list[Source] = []
        for raw_source in data.get("sources") or []:
            source_data = dict(raw_source)
            if not source_data.get("project_id") and source_data.get("project_name"):
                name = str(source_data["project_name"]).strip().casefold()
                project_id = project_ids.get(name)
                if not project_id:
                    raise ValueError(
                        f"source references unknown project: {source_data['project_name']}"
                    )
                source_data["project_id"] = project_id
            sources.append(Source.from_dict(source_data))
        return cls(
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            competition_id=str(data.get("competition_id") or f"cmp_{uuid.uuid4().hex[:12]}"),
            timezone_name=str(data.get("timezone_name") or "UTC"),
            starts_at=str(data.get("starts_at") or ""),
            ends_at=str(data.get("ends_at") or ""),
            projects=projects,
            sources=sources,
            methods=list(data.get("methods") or []),
            eligibility=EligibilityRules.from_dict(data.get("eligibility")),
            evidence=EvidenceRules.from_dict(data.get("evidence")),
            ranking=RankingRules.from_dict(data.get("ranking")),
            collection=CollectionSettings.from_dict(data.get("collection")),
            status=str(data.get("status") or "collecting"),
            ruleset_version=str(data.get("ruleset_version") or ""),
        )


@dataclass
class NormalizedEvent:
    event_id: str
    competition_id: str
    project_id: str
    voting_method: str
    source_type: str
    source_id: str
    source_url: str
    source_tool: str
    auth_tier: str
    account_id: str
    event_timestamp: str
    collected_at: str
    raw_evidence_reference: str
    normalized_value: dict[str, Any]
    eligibility_status: str
    verification_status: str
    confidence_label: str
    decision_reason: str
    ruleset_version: str
    review_status: str
    individual_identity_available: bool
    dedupe_key: str = ""
    superseded_by: str = ""
    manual_decision: bool = False
    limitations: list[str] = field(default_factory=list)
    is_test_data: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "competition_id": self.competition_id,
            "project_id": self.project_id,
            "voting_method": self.voting_method,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_tool": self.source_tool,
            "auth_tier": self.auth_tier,
            "account_id": self.account_id,
            "event_timestamp": self.event_timestamp,
            "collected_at": self.collected_at,
            "raw_evidence_reference": self.raw_evidence_reference,
            "normalized_value": dict(self.normalized_value),
            "eligibility_status": self.eligibility_status,
            "verification_status": self.verification_status,
            "confidence_label": self.confidence_label,
            "decision_reason": self.decision_reason,
            "ruleset_version": self.ruleset_version,
            "review_status": self.review_status,
            "individual_identity_available": self.individual_identity_available,
            "dedupe_key": self.dedupe_key,
            "superseded_by": self.superseded_by,
            "manual_decision": self.manual_decision,
            "limitations": list(self.limitations),
            "is_test_data": self.is_test_data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NormalizedEvent:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {key: value for key, value in data.items() if key in known}
        return cls(**payload)


@dataclass
class PollAggregate:
    aggregate_id: str
    competition_id: str
    project_id: str
    source_id: str
    source_url: str
    source_tool: str
    auth_tier: str
    option_label: str
    votes: int
    total_votes: int
    finished: bool
    collected_at: str
    raw_evidence_reference: str
    confidence_label: str
    verification_status: str
    decision_reason: str
    ruleset_version: str
    manually_supplied: bool = False
    voting_method: str = VotingMethod.NATIVE_POLL.value
    is_test_data: bool = False
    manual_decision: bool = False
    eligibility_status: str = EligibilityStatus.ELIGIBLE.value
    history: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def individual_identity_available(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregate_id": self.aggregate_id,
            "competition_id": self.competition_id,
            "project_id": self.project_id,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_tool": self.source_tool,
            "auth_tier": self.auth_tier,
            "option_label": self.option_label,
            "votes": self.votes,
            "total_votes": self.total_votes,
            "finished": self.finished,
            "collected_at": self.collected_at,
            "raw_evidence_reference": self.raw_evidence_reference,
            "confidence_label": self.confidence_label,
            "verification_status": self.verification_status,
            "decision_reason": self.decision_reason,
            "ruleset_version": self.ruleset_version,
            "manually_supplied": self.manually_supplied,
            "voting_method": self.voting_method,
            "is_test_data": self.is_test_data,
            "manual_decision": self.manual_decision,
            "eligibility_status": self.eligibility_status,
            "history": [dict(item) for item in self.history],
            "limitations": list(self.limitations),
            "individual_identity_available": False,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PollAggregate:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {key: value for key, value in data.items() if key in known}
        return cls(**payload)


@dataclass
class AuditRecord:
    audit_id: str
    timestamp: str
    actor: str
    action: str
    competition_id: str
    event_id: str = ""
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditRecord:
        return cls(
            audit_id=str(data.get("audit_id") or ""),
            timestamp=str(data.get("timestamp") or ""),
            actor=str(data.get("actor") or ""),
            action=str(data.get("action") or ""),
            competition_id=str(data.get("competition_id") or ""),
            event_id=str(data.get("event_id") or ""),
            before=dict(data.get("before") or {}),
            after=dict(data.get("after") or {}),
            note=str(data.get("note") or ""),
        )


@dataclass
class SourceCoverage:
    source_id: str
    source_type: str
    label: str
    collected_methods: list[str] = field(default_factory=list)
    unavailable_methods: list[str] = field(default_factory=list)
    individual_voters_identifiable: bool = False
    exact_counts_available: bool = False
    partial: bool = False
    manually_supplied: bool = False
    last_checked: str = ""
    collection_status: str = CollectionStatus.NOT_STARTED.value
    limitations: list[str] = field(default_factory=list)
    adapter_attempts: list[dict[str, Any]] = field(default_factory=list)
    post_author_username: str = ""
    post_url: str = ""
    post_created_at: str = ""
    post_metrics: dict[str, int] = field(default_factory=dict)
    post_metrics_collected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RankingEntry:
    rank: int
    project_id: str
    name: str
    external_id: str = ""
    score: float = 0.0
    confirmed_votes: int = 0
    confirmed_individual: int = 0
    confirmed_aggregate: int = 0
    raw_activity: int = 0
    provisional_votes: int = 0
    rejected_or_late_activity: int = 0
    pending_review: int = 0
    ambiguous: int = 0
    vote_share: float | None = None
    vote_share_denominator: int | None = None
    breakdown: dict[str, int] = field(default_factory=dict)
    aggregate_breakdown: dict[str, int] = field(default_factory=dict)
    confidence_breakdown: dict[str, int] = field(default_factory=dict)
    account_count: int = 0
    coverage_status: str = CollectionStatus.NOT_STARTED.value
    unresolved_count: int = 0
    last_calculated: str = ""
    finalization_status: str = "collecting"
    limitations: list[str] = field(default_factory=list)
    weighted_inputs: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Leaderboard:
    competition_id: str
    competition_name: str
    view: str
    mode: str
    generated_at: str
    ruleset_version: str
    entries: list[RankingEntry] = field(default_factory=list)
    total_confirmed: int = 0
    denominator: int | None = None
    tie_rule: str = "competition"
    coverage_status: str = CollectionStatus.NOT_STARTED.value
    limitations: list[str] = field(default_factory=list)
    finalization_status: str = "collecting"
    formula: str = ""
    is_test_data: bool = False
    metric: str = ""
    metric_description: str = ""
    score_label: str = ""
    denominator_kind: str = ""
    confirmed_individual_total: int = 0
    confirmed_aggregate_total: int = 0
    total_unresolved: int = 0
    unassigned_events: int = 0
    unassigned_pending_review: int = 0
    unassigned_rejected: int = 0
    aggregates_ranked_separately: bool = True
    sources: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **{key: value for key, value in self.__dict__.items() if key != "entries"},
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass
class Snapshot:
    snapshot_id: str
    competition_id: str
    created_at: str
    ruleset_version: str
    view: str
    ranks: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    coverage_status: str = CollectionStatus.NOT_STARTED.value
    collection_status: str = CollectionStatus.NOT_STARTED.value

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TaskMessage:
    task_id: str
    parent_task_id: str
    agent_role: str
    competition_id: str
    input_reference_ids: list[str]
    ruleset_version: str
    requested_operation: str
    status: str = "pending"
    result: dict[str, Any] = field(default_factory=dict)
    confidence: str = Confidence.INSUFFICIENT_EVIDENCE.value
    evidence_references: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    error_code: str = ""
    started_at: str = ""
    duration_ms: int = 0
    attempts: int = 0
    resource_usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


ALL_STATUSES = [item.value for item in CollectionStatus]
