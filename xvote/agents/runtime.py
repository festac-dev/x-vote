"""Bounded, deterministic multi-agent runtime with optional AI suggestions.

The runtime is a logical hierarchy (orchestrator → workers → narrow checks) that
may share one process. Deterministic code remains authoritative for collection,
counting, deduplication, eligibility and ranking. Model output can only ever be
recorded as a suggestion.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..config import AcquisitionConfig
from ..models import (
    Competition,
    Confidence,
    Leaderboard,
    NormalizedEvent,
    PollAggregate,
    SourceCoverage,
    TaskMessage,
    VotingMethod,
    canonical_json,
    stable_hash,
)
from ..textmatch import suggest_intent
from .providers import ModelProvider, NoAIProvider, ProviderError

TASK_STATUSES = {"pending", "running", "succeeded", "failed", "timed_out", "cancelled", "skipped"}

ALLOWED_RESULT_KEYS = {
    "orchestrator": {"plan", "task_count", "depth_limit"},
    "alias_matching": {"projects", "identifier_count", "shared_identifiers"},
    "classification": {
        "events_examined",
        "suggestions",
        "model_used",
        "model_failures",
        "model_available",
    },
    "duplicate_analysis": {"duplicate_keys", "superseded", "authority"},
    "eligibility_verification": {"eligible", "ineligible", "pending", "authority"},
    "evidence_audit": {"missing_reference", "missing_url", "missing_timestamp", "events_examined"},
    "poll_reconciliation": {
        "aggregates",
        "unassigned",
        "inconsistent_totals",
        "never_manufactures_votes",
    },
    "source_metadata_validator": {"sources", "warnings"},
    "event_consistency_checker": {"unknown_projects", "future_timestamps", "duplicate_ids"},
    "eligibility_boundary_checker": {"boundary_cases", "deadline", "opening"},
    "leaderboard_verifier": {"projects", "matches", "confirmed_total", "rank_mode"},
    "quality_assurance": {"coverage_status", "warning_count", "test_data", "collection_status"},
    "report_generation": {"summary", "projects", "confirmed_total", "provisional_total"},
    "export_integrity_checker": {"findings"},
}


class AgentConfigurationError(ValueError):
    pass


class InvalidAgentOutput(ValueError):
    pass


def validate_result(role: str, result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise InvalidAgentOutput("agent result must be an object")
    allowed = ALLOWED_RESULT_KEYS.get(role)
    if allowed is None:
        raise InvalidAgentOutput(f"unknown agent role: {role}")
    unexpected = set(result) - allowed
    if unexpected:
        raise InvalidAgentOutput(
            "agent returned unsupported result fields: " + ", ".join(sorted(unexpected))
        )
    try:
        json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        raise InvalidAgentOutput("agent result is not JSON-serializable") from exc
    return result


# ---------------------------------------------------------------- narrow checks


def validate_source_metadata(competition: Competition) -> dict[str, Any]:
    warnings: list[str] = []
    for source in competition.sources:
        if source.source_type == "x_post" and not source.post_id and not source.url:
            warnings.append(f"Source {source.source_id} has neither a post ID nor a URL.")
        if source.project_id and not competition.project_by_id(source.project_id):
            warnings.append(f"Source {source.source_id} references an unknown project.")
    return {"sources": len(competition.sources), "warnings": warnings}


def check_event_consistency(
    competition: Competition, events: list[NormalizedEvent]
) -> dict[str, Any]:
    known = {project.project_id for project in competition.projects}
    unknown = 0
    future = 0
    duplicate_ids = 0
    seen: set[str] = set()
    now = datetime.now(UTC)
    for event in events:
        if event.project_id and event.project_id not in known:
            unknown += 1
        if event.event_id in seen:
            duplicate_ids += 1
        seen.add(event.event_id)
        try:
            timestamp = datetime.fromisoformat(event.event_timestamp.replace("Z", "+00:00"))
            if timestamp > now:
                future += 1
        except ValueError:
            continue
    return {
        "unknown_projects": unknown,
        "future_timestamps": future,
        "duplicate_ids": duplicate_ids,
    }


def check_eligibility_boundaries(
    competition: Competition, events: list[NormalizedEvent]
) -> dict[str, Any]:
    boundary = [
        event.event_id
        for event in events
        if event.eligibility_status in {"pending_eligibility_review", "eligibility_unknown"}
    ]
    return {
        "boundary_cases": len(boundary),
        "deadline": competition.ends_at,
        "opening": competition.starts_at,
    }


def reconcile_poll(competition: Competition, polls: list[PollAggregate]) -> dict[str, Any]:
    unassigned = 0
    inconsistent = 0
    for poll in polls:
        if not poll.project_id:
            unassigned += 1
        if (
            int(poll.votes) < 0
            or int(poll.total_votes) < 0
            or int(poll.total_votes) < int(poll.votes)
        ):
            inconsistent += 1
    return {
        "aggregates": len(polls),
        "unassigned": unassigned,
        "inconsistent_totals": inconsistent,
        "never_manufactures_votes": True,
    }


def verify_leaderboard(
    competition: Competition,
    leaderboard: Leaderboard,
    events: list[NormalizedEvent],
    polls: list[PollAggregate],
) -> dict[str, Any]:
    active = [
        event
        for event in events
        if not event.superseded_by and event.ruleset_version == competition.ruleset_version
    ]
    confirmed = sum(
        1
        for event in active
        if event.eligibility_status == "eligible" and event.verification_status == "verified"
    )
    aggregate = sum(
        poll.votes
        for poll in polls
        if poll.project_id
        and poll.verification_status in {"verified", "not_individually_verifiable"}
        and poll.eligibility_status == "eligible"
    )
    expected = confirmed + aggregate
    actual = sum(entry.confirmed_votes for entry in leaderboard.entries)
    return {
        "projects": len(leaderboard.entries),
        "matches": expected == actual,
        "confirmed_total": actual,
        "rank_mode": competition.ranking.mode,
    }


def check_export_integrity(package_bytes: bytes, leaderboard: Leaderboard) -> dict[str, Any]:
    findings: list[str] = []
    lowered = package_bytes.decode("utf-8", errors="replace").lower()
    for marker in ("authorization:", "auth_token", "session_cookie", "password"):
        if marker in lowered:
            findings.append(f"export contains a secret-like marker: {marker}")
    return {"findings": findings}


# ---------------------------------------------------------------- runtime


@dataclass
class _TaskSpec:
    role: str
    operation: str
    depth: int
    parent_task_id: str = ""
    run: Callable[[], Awaitable[dict[str, Any]]] | None = None


class AgentRuntime:
    def __init__(
        self,
        config: AcquisitionConfig,
        provider: ModelProvider | None = None,
    ) -> None:
        self.config = config
        self.max_concurrent = config.agents_max_concurrent
        self.max_depth = config.agents_max_depth
        self.retry_limit = config.retry_limit
        self.timeout = max(5.0, config.per_task_timeout_seconds)
        self.provider = provider or NoAIProvider()
        self.tasks: list[TaskMessage] = []
        self._completed: dict[str, TaskMessage] = {}
        self._cancelled = False
        self._paused = False
        self._lock = asyncio.Lock()

    def cancel(self) -> None:
        self._cancelled = True
        self._paused = False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    async def run_plan(
        self,
        competition: Competition,
        events: list[NormalizedEvent],
        polls: list[PollAggregate],
        coverage: dict[str, SourceCoverage],
        leaderboard: Leaderboard,
    ) -> list[TaskMessage]:
        self._cancelled = False
        self._paused = False
        input_hash = self._input_hash(competition, events, polls)
        specs = self._build_specs(competition, events, polls, coverage, leaderboard, input_hash)
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def run_spec(spec: _TaskSpec) -> TaskMessage:
            return await self._run_spec(spec, competition, input_hash, semaphore)

        results = await asyncio.gather(*(run_spec(spec) for spec in specs))
        async with self._lock:
            self.tasks.extend(results)
        return list(results)

    def _input_hash(
        self, competition: Competition, events: list[NormalizedEvent], polls: list[PollAggregate]
    ) -> str:
        digest_input = [
            (
                event.event_id,
                event.project_id,
                event.eligibility_status,
                event.verification_status,
                event.decision_reason,
                event.review_status,
                event.superseded_by,
            )
            for event in sorted(events, key=lambda item: item.event_id)
        ]
        poll_digest = [
            (poll.aggregate_id, poll.project_id, poll.votes, poll.verification_status)
            for poll in sorted(polls, key=lambda item: item.aggregate_id)
        ]
        return stable_hash(
            competition.competition_id,
            competition.ruleset_version,
            canonical_json(digest_input),
            canonical_json(poll_digest),
            length=20,
        )

    def _build_specs(
        self,
        competition: Competition,
        events: list[NormalizedEvent],
        polls: list[PollAggregate],
        coverage: dict[str, SourceCoverage],
        leaderboard: Leaderboard,
        input_hash: str,
    ) -> list[_TaskSpec]:
        specs: list[_TaskSpec] = []
        orchestrator_id = f"tsk_orch_{input_hash[:10]}"
        plan_roles = [
            "alias_matching",
            "source_metadata_validator",
            "classification",
            "duplicate_analysis",
            "eligibility_verification",
            "eligibility_boundary_checker",
            "event_consistency_checker",
            "poll_reconciliation",
            "evidence_audit",
            "leaderboard_verifier",
            "quality_assurance",
            "report_generation",
        ]
        specs.append(
            _TaskSpec(
                role="orchestrator",
                operation="build_task_plan",
                depth=0,
                run=lambda: _async_result(
                    {
                        "plan": plan_roles,
                        "task_count": len(plan_roles) + 1,
                        "depth_limit": self.max_depth,
                    }
                ),
            )
        )
        for role in plan_roles:
            depth = 2 if role in {"event_consistency_checker", "leaderboard_verifier"} else 1
            if depth >= self.max_depth:
                raise AgentConfigurationError(
                    f"agent plan exceeds configured max depth for role {role}"
                )
            parent = orchestrator_id
            specs.append(
                _TaskSpec(
                    role=role,
                    operation=role,
                    depth=depth,
                    parent_task_id=parent,
                    run=self._worker_for_role(
                        role, competition, events, polls, coverage, leaderboard
                    ),
                )
            )
        return specs

    def _worker_for_role(
        self,
        role: str,
        competition: Competition,
        events: list[NormalizedEvent],
        polls: list[PollAggregate],
        coverage: dict[str, SourceCoverage],
        leaderboard: Leaderboard,
    ) -> Callable[[], Awaitable[dict[str, Any]]]:
        active = [event for event in events if not event.superseded_by]

        if role == "alias_matching":

            async def run() -> dict[str, Any]:
                identifiers: dict[str, list[str]] = {}
                for project in competition.projects:
                    for identifier in project.identifiers():
                        identifiers.setdefault(identifier.casefold(), []).append(project.name)
                shared = {key: names for key, names in identifiers.items() if len(set(names)) > 1}
                return {
                    "projects": len(competition.projects),
                    "identifier_count": len(identifiers),
                    "shared_identifiers": sorted(shared),
                }

            return run

        if role == "classification":

            async def run() -> dict[str, Any]:
                suggestions: dict[str, int] = {}
                model_used = False
                model_failures = 0
                model_calls = 0
                available, _ = self.provider.available()
                for event in active:
                    if event.voting_method not in {
                        VotingMethod.REPLY.value,
                        VotingMethod.QUOTE.value,
                        VotingMethod.MENTION.value,
                    }:
                        continue
                    text = str(event.normalized_value.get("text_excerpt") or "")
                    deterministic = suggest_intent(text)
                    result = deterministic.to_dict()
                    if (
                        available
                        and model_calls < 25
                        and deterministic.classification in {"ambiguous", "question"}
                        and text
                    ):
                        model_calls += 1
                        try:
                            suggestion = await asyncio.wait_for(
                                self.provider.classify(
                                    text[:500], [project.name for project in competition.projects]
                                ),
                                timeout=min(15.0, self.timeout),
                            )
                            result["model_suggestion"] = suggestion
                            model_used = True
                        except (TimeoutError, ProviderError, ValueError) as exc:
                            model_failures += 1
                            event.normalized_value["model_error"] = type(exc).__name__
                    event.normalized_value["classification_suggestion"] = result
                    classification = str(result.get("classification") or "ambiguous")
                    suggestions[classification] = suggestions.get(classification, 0) + 1
                return {
                    "events_examined": len(active),
                    "suggestions": suggestions,
                    "model_used": model_used,
                    "model_failures": model_failures,
                    "model_available": available,
                }

            return run

        if role == "duplicate_analysis":

            async def run() -> dict[str, Any]:
                keys: dict[str, int] = {}
                for event in events:
                    keys[event.dedupe_key or event.event_id] = (
                        keys.get(event.dedupe_key or event.event_id, 0) + 1
                    )
                duplicate_keys = {key: count for key, count in keys.items() if count > 1}
                superseded = sum(1 for event in events if event.superseded_by)
                return {
                    "duplicate_keys": len(duplicate_keys),
                    "superseded": superseded,
                    "authority": "deterministic-dedupe-engine",
                }

            return run

        if role == "eligibility_verification":

            async def run() -> dict[str, Any]:
                counts = {"eligible": 0, "ineligible": 0, "pending": 0}
                for event in active:
                    if event.eligibility_status == "eligible":
                        counts["eligible"] += 1
                    elif event.eligibility_status == "ineligible":
                        counts["ineligible"] += 1
                    else:
                        counts["pending"] += 1
                return {**counts, "authority": "rules-engine"}

            return run

        if role == "evidence_audit":

            async def run() -> dict[str, Any]:
                missing_reference = sum(1 for event in active if not event.raw_evidence_reference)
                missing_url = sum(1 for event in active if not event.source_url)
                missing_timestamp = sum(1 for event in active if not event.event_timestamp)
                return {
                    "missing_reference": missing_reference,
                    "missing_url": missing_url,
                    "missing_timestamp": missing_timestamp,
                    "events_examined": len(active),
                }

            return run

        if role == "poll_reconciliation":

            async def run() -> dict[str, Any]:
                return reconcile_poll(competition, polls)

            return run

        if role == "source_metadata_validator":

            async def run() -> dict[str, Any]:
                return validate_source_metadata(competition)

            return run

        if role == "event_consistency_checker":

            async def run() -> dict[str, Any]:
                return check_event_consistency(competition, events)

            return run

        if role == "eligibility_boundary_checker":

            async def run() -> dict[str, Any]:
                return check_eligibility_boundaries(competition, active)

            return run

        if role == "leaderboard_verifier":

            async def run() -> dict[str, Any]:
                return verify_leaderboard(competition, leaderboard, events, polls)

            return run

        if role == "quality_assurance":

            async def run() -> dict[str, Any]:
                warnings = 0
                for cov in coverage.values():
                    warnings += len(cov.limitations)
                    if cov.partial:
                        warnings += 1
                return {
                    "coverage_status": leaderboard.coverage_status,
                    "warning_count": warnings,
                    "test_data": leaderboard.is_test_data,
                    "collection_status": leaderboard.coverage_status,
                }

            return run

        if role == "report_generation":

            async def run() -> dict[str, Any]:
                return {
                    "summary": (
                        f"{len(competition.projects)} project(s), "
                        f"{len(active)} active observation(s), "
                        f"{leaderboard.total_confirmed} confirmed vote(s); "
                        f"coverage {leaderboard.coverage_status}."
                    ),
                    "projects": len(competition.projects),
                    "confirmed_total": leaderboard.total_confirmed,
                    "provisional_total": sum(
                        entry.provisional_votes for entry in leaderboard.entries
                    ),
                }

            return run

        async def unsupported() -> dict[str, Any]:
            raise InvalidAgentOutput(f"unsupported agent role: {role}")

        return unsupported

    async def _run_spec(
        self,
        spec: _TaskSpec,
        competition: Competition,
        input_hash: str,
        semaphore: asyncio.Semaphore,
    ) -> TaskMessage:
        signature = stable_hash(
            competition.competition_id, input_hash, spec.role, spec.operation, length=24
        )
        task = TaskMessage(
            task_id=f"tsk_{signature}",
            parent_task_id=spec.parent_task_id or f"tsk_orch_{input_hash[:10]}",
            agent_role=spec.role,
            competition_id=competition.competition_id,
            input_reference_ids=[input_hash],
            ruleset_version=competition.ruleset_version,
            requested_operation=spec.operation,
        )
        if signature in self._completed:
            previous = self._completed[signature]
            task.status = "skipped"
            task.result = previous.result
            task.confidence = previous.confidence
            task.limitations = ["Unchanged inputs; previous result reused."]
            return task
        while self._paused and not self._cancelled:
            await asyncio.sleep(0.05)
        if self._cancelled:
            task.status = "cancelled"
            task.error_code = "cancelled"
            return task
        if spec.run is None:
            task.status = "failed"
            task.error_code = "no_worker"
            return task
        async with semaphore:
            while self._paused and not self._cancelled:
                await asyncio.sleep(0.05)
            if self._cancelled:
                task.status = "cancelled"
                task.error_code = "cancelled"
                return task
            task.status = "running"
            task.started_at = datetime.now(UTC).isoformat(timespec="seconds")
            started = asyncio.get_running_loop().time()
            attempts = 0
            while attempts <= self.retry_limit:
                attempts += 1
                try:
                    result = await asyncio.wait_for(spec.run(), timeout=self.timeout)
                    validate_result(spec.role, result)
                    task.result = result
                    task.status = "succeeded"
                    task.confidence = Confidence.MODERATE.value
                    task.limitations = []
                    if spec.role == "classification" and result.get("model_failures"):
                        task.limitations.append(
                            "Some model suggestions were unavailable; "
                            "deterministic classification was retained."
                        )
                    break
                except TimeoutError:
                    task.status = "timed_out"
                    task.error_code = "timeout"
                except InvalidAgentOutput as exc:
                    task.status = "failed"
                    task.error_code = "invalid_output"
                    task.result = {"error": str(exc)[:300]}
                    break
                except asyncio.CancelledError:
                    task.status = "cancelled"
                    task.error_code = "cancelled"
                    raise
                except Exception as exc:
                    task.status = "failed"
                    task.error_code = "worker_error"
                    task.result = {"error": type(exc).__name__}
            if (
                task.status not in {"succeeded"}
                and task.error_code == "timeout"
                and attempts > self.retry_limit
            ):
                task.status = "timed_out"
            task.duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            task.attempts = attempts
            task.resource_usage = {
                "mode": "in-process",
                "model_calls": int(task.result.get("model_calls", 0))
                if isinstance(task.result, dict)
                else 0,
            }
        if task.status == "succeeded":
            self._completed[signature] = task
        return task


async def _async_result(value: dict[str, Any]) -> dict[str, Any]:
    return value


def run_agents_sync(
    config: AcquisitionConfig,
    competition: Competition,
    events: list[NormalizedEvent],
    polls: list[PollAggregate],
    coverage: dict[str, SourceCoverage],
    leaderboard: Leaderboard,
    provider: ModelProvider | None = None,
) -> list[TaskMessage]:
    runtime = AgentRuntime(config, provider)
    return asyncio.run(runtime.run_plan(competition, events, polls, coverage, leaderboard))
