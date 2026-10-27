"""Deterministic ranking, safe formulas, snapshots and timeline."""

from __future__ import annotations

import ast
import operator
from datetime import datetime, timedelta
from typing import Any

from .models import (
    AGGREGATE_METHODS,
    CollectionStatus,
    Competition,
    EligibilityStatus,
    Leaderboard,
    NormalizedEvent,
    PollAggregate,
    Project,
    RankingEntry,
    Snapshot,
    SourceCoverage,
    VerificationStatus,
    VotingMethod,
    isoformat,
    stable_hash,
    utc_now,
)

COUNTED_AGGREGATE_VERIFICATION = {
    VerificationStatus.NOT_INDIVIDUALLY_VERIFIABLE.value,
    VerificationStatus.VERIFIED.value,
}
UNRESOLVED_VERIFICATION = {
    VerificationStatus.PENDING_REVIEW.value,
    VerificationStatus.DISPUTED.value,
    VerificationStatus.INCONCLUSIVE.value,
    VerificationStatus.NOT_INDIVIDUALLY_VERIFIABLE.value,
}

ALLOWED_BINOPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
ALLOWED_UNARY: dict[type[ast.unaryop], Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class FormulaError(ValueError):
    pass


def _eval_node(node: ast.AST, variables: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise FormulaError(f"unknown formula variable: {node.id}")
        return float(variables[node.id])
    if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_BINOPS:
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        if isinstance(node.op, ast.Div) and right == 0:
            return 0.0
        return float(ALLOWED_BINOPS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in ALLOWED_UNARY:
        return float(ALLOWED_UNARY[type(node.op)](_eval_node(node.operand, variables)))
    raise FormulaError("formula contains an unsupported expression")


def safe_formula(formula: str, variables: dict[str, float]) -> float:
    """Evaluate a small arithmetic formula without eval/exec."""
    if not formula.strip():
        raise FormulaError("custom formula is empty")
    if len(formula) > 300:
        raise FormulaError("custom formula is too long")
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError("custom formula is not valid arithmetic") from exc
    return _eval_node(tree, variables)


def validate_formula(formula: str, method_names: list[str] | None = None) -> None:
    variables = {
        "confirmed": 1.0,
        "provisional": 1.0,
        "raw": 1.0,
        "accounts": 1.0,
        "poll": 1.0,
        "manual": 1.0,
        "ocr": 1.0,
        "external": 1.0,
    }
    for method in method_names or []:
        variables[method] = 1.0
    safe_formula(formula, variables)


def _aggregate_status(polls: list[PollAggregate]) -> str:
    if not polls:
        return CollectionStatus.NOT_STARTED.value
    if any(poll.verification_status == VerificationStatus.PENDING_REVIEW.value for poll in polls):
        return CollectionStatus.PARTIALLY_COMPLETE.value
    if any(poll.verification_status == VerificationStatus.REJECTED.value for poll in polls):
        return CollectionStatus.PARTIALLY_COMPLETE.value
    return CollectionStatus.COMPLETE_FOR_SCOPE.value


def _overall_coverage(
    competition: Competition, coverage: dict[str, SourceCoverage]
) -> tuple[str, list[str]]:
    source_coverages = [coverage.get(source.source_id) for source in competition.sources]
    limitations: list[str] = []
    statuses: list[str] = []
    for source, cov in zip(competition.sources, source_coverages, strict=True):
        if cov is None:
            statuses.append(CollectionStatus.NOT_STARTED.value)
            limitations.append(f"Source {source.label or source.source_id} was never checked.")
            continue
        statuses.append(cov.collection_status)
        for limitation in cov.limitations:
            if limitation not in limitations:
                limitations.append(limitation)
    if not statuses or all(status == CollectionStatus.NOT_STARTED.value for status in statuses):
        overall = CollectionStatus.NOT_STARTED.value
    elif any(
        status
        in {
            CollectionStatus.FAILED.value,
            CollectionStatus.SOURCE_UNAVAILABLE.value,
            CollectionStatus.PERMISSION_RESTRICTED.value,
            CollectionStatus.RATE_LIMITED.value,
        }
        for status in statuses
    ):
        overall = CollectionStatus.PARTIALLY_COMPLETE.value
    elif any(
        status
        in {
            CollectionStatus.PARTIALLY_COMPLETE.value,
            CollectionStatus.MANUALLY_SUPPLIED.value,
        }
        for status in statuses
    ):
        overall = CollectionStatus.PARTIALLY_COMPLETE.value
    else:
        overall = CollectionStatus.COMPLETE_FOR_SCOPE.value
    if competition.sources:
        limitations.append(
            "Coverage reflects the configured sources only; "
            "activities outside those sources are not observed."
        )
    return overall, limitations


def _entry_limitations(competition: Competition, coverage: dict[str, SourceCoverage]) -> list[str]:
    limitations: list[str] = []
    method_coverage: dict[str, list[str]] = {}
    for source in competition.sources:
        cov = coverage.get(source.source_id)
        if not cov:
            continue
        for method in source.methods or competition.methods:
            method_coverage.setdefault(method, []).append(cov.collection_status)
    for method in competition.methods:
        statuses = method_coverage.get(method, [])
        if not statuses or all(
            status
            in {
                CollectionStatus.NOT_STARTED.value,
                CollectionStatus.SOURCE_UNAVAILABLE.value,
                CollectionStatus.FAILED.value,
            }
            for status in statuses
        ):
            limitations.append(
                f"Method '{method}' was not successfully collected for this source set."
            )
    if (
        VotingMethod.LIKE.value in competition.methods
        or VotingMethod.REPOST.value in competition.methods
    ):
        limitations.append(
            "Account activity counts accounts, not unique humans; "
            "multi-account participation is not distinguishable."
        )
    if VotingMethod.NATIVE_POLL.value in competition.methods:
        limitations.append(
            "Anonymous poll aggregates are counted separately from individually observed events."
        )
    return limitations


def _aggregate_counts(
    project_id: str, polls: list[PollAggregate]
) -> tuple[int, int, dict[str, int], dict[str, int], int]:
    confirmed = 0
    provisional = 0
    breakdown: dict[str, int] = {}
    confidence: dict[str, int] = {}
    raw = 0
    for poll in polls:
        if poll.project_id != project_id:
            continue
        method = poll.voting_method or VotingMethod.NATIVE_POLL.value
        if (
            poll.verification_status in COUNTED_AGGREGATE_VERIFICATION
            and poll.eligibility_status == EligibilityStatus.ELIGIBLE.value
        ):
            confirmed += poll.votes
            breakdown[method] = breakdown.get(method, 0) + poll.votes
            confidence[poll.confidence_label] = (
                confidence.get(poll.confidence_label, 0) + poll.votes
            )
            raw += poll.votes
        elif poll.verification_status in {
            VerificationStatus.PENDING_REVIEW.value,
            VerificationStatus.INCONCLUSIVE.value,
        }:
            provisional += poll.votes
            raw += poll.votes
        elif poll.verification_status == VerificationStatus.DISPUTED.value:
            provisional += poll.votes
            raw += poll.votes
        else:
            raw += poll.votes
    return confirmed, provisional, breakdown, confidence, raw


def _score_entry(
    competition: Competition,
    project: Project,
    entry: RankingEntry,
    confirmed_by_method: dict[str, int],
    aggregate_by_method: dict[str, int],
    confirmed_accounts: set[str],
    *,
    combined_allowed: bool,
) -> tuple[float, str, dict[str, float]]:
    rules = competition.ranking
    variables: dict[str, float] = {
        "confirmed": float(entry.confirmed_votes),
        "provisional": float(entry.provisional_votes),
        "raw": float(entry.raw_activity),
        "accounts": float(len(confirmed_accounts)),
    }
    for method, count in confirmed_by_method.items():
        variables[method] = float(count)
    for method, count in aggregate_by_method.items():
        key = (
            "poll"
            if method == VotingMethod.NATIVE_POLL.value
            else (
                "manual"
                if method == VotingMethod.MANUAL_TOTAL.value
                else ("ocr" if method == VotingMethod.OCR_TOTAL.value else "external")
            )
        )
        variables[key] = variables.get(key, 0.0) + float(count)
        variables[method] = float(count)
    if rules.mode == "unique_account":
        score = float(len(confirmed_accounts))
        formula = (
            "distinct observed account IDs among confirmed individual events "
            "(anonymous aggregates excluded)"
        )
    elif rules.mode == "weighted":
        weights = {
            key: rules.weights.get(key, 1.0)
            for key in set(confirmed_by_method) | set(aggregate_by_method)
        }
        score = sum(
            confirmed_by_method.get(method, 0) * weight
            for method, weight in weights.items()
            if method not in AGGREGATE_METHODS
        ) + sum(
            aggregate_by_method.get(method, 0) * weight
            for method, weight in weights.items()
            if method in AGGREGATE_METHODS
        )
        formula = (
            " + ".join(f"{method}×{weight:g}" for method, weight in sorted(weights.items()))
            or "no counts"
        )
        variables.update({f"weighted_{key}": value for key, value in weights.items()})
    elif rules.mode == "custom":
        try:
            score = safe_formula(rules.custom_formula, variables)
        except FormulaError:
            score = float(entry.confirmed_individual)
        formula = rules.custom_formula
    elif rules.mode == "separate":
        score = float(entry.confirmed_individual)
        formula = (
            "separate counts only: ranking uses confirmed individual events; "
            "aggregate totals are reported separately and never merged into the score"
        )
    else:
        if combined_allowed:
            score = float(entry.confirmed_votes)
            formula = (
                "confirmed = confirmed individual events + confirmed source-reported aggregate "
                "votes (operator explicitly approved this combination)"
            )
        else:
            score = float(entry.confirmed_individual)
            formula = (
                "confirmed individual events only; source-reported aggregate votes "
                "(poll/manual/external/OCR) are shown separately and excluded from the score"
            )
    for key, value in variables.items():
        entry.weighted_inputs[key] = round(value, 4)
    return score, formula, variables


def build_leaderboard(
    competition: Competition,
    events: list[NormalizedEvent],
    polls: list[PollAggregate],
    coverage: dict[str, SourceCoverage],
    *,
    view: str = "confirmed",
    is_test_data: bool = False,
) -> Leaderboard:
    if view not in {"confirmed", "provisional", "raw", "method", "account", "weighted", "timeline"}:
        raise ValueError("unsupported leaderboard view")
    has_aggregate_methods = any(method in AGGREGATE_METHODS for method in competition.methods)
    has_individual_methods = any(method not in AGGREGATE_METHODS for method in competition.methods)
    mixed_methods = has_aggregate_methods and has_individual_methods
    combined_allowed = (
        not mixed_methods
        or competition.ranking.mode in {"weighted", "custom", "unique_account"}
        or competition.ranking.combine_confirmed_with_aggregates
    )
    overall_coverage, overall_limitations = _overall_coverage(competition, coverage)
    if mixed_methods and not combined_allowed:
        overall_limitations.append(
            "This competition mixes source-reported aggregate votes with individually evidenced "
            "activity. The default ranking counts only individually evidenced confirmed events; "
            "aggregate totals are reported separately. To combine them, the operator must "
            "explicitly configure ranking.combine_confirmed_with_aggregates, a weighted mode, or a "
            "documented custom formula."
        )
    active_events = [
        event
        for event in events
        if event.competition_id == competition.competition_id and not event.superseded_by
    ]
    stale = [
        event for event in active_events if event.ruleset_version != competition.ruleset_version
    ]
    if stale:
        overall_limitations.append(
            f"{len(stale)} event(s) were collected under a different ruleset "
            "version and are excluded. "
            "Run collection again after changing rules."
        )
    active_events = [
        event for event in active_events if event.ruleset_version == competition.ruleset_version
    ]
    active_polls = [
        poll
        for poll in polls
        if poll.competition_id == competition.competition_id
        and poll.ruleset_version in {"", competition.ruleset_version}
    ]
    unassigned_polls = [poll for poll in active_polls if not poll.project_id]
    if unassigned_polls:
        overall_limitations.append(
            f"{len(unassigned_polls)} poll option observation(s) could not be mapped to a project."
        )
    unassigned_events = [event for event in active_events if not event.project_id]
    unassigned_pending = sum(
        1
        for event in unassigned_events
        if event.verification_status
        in {
            VerificationStatus.PENDING_REVIEW.value,
            VerificationStatus.DISPUTED.value,
            VerificationStatus.INCONCLUSIVE.value,
        }
        or event.eligibility_status
        in {
            EligibilityStatus.PENDING_ELIGIBILITY_REVIEW.value,
            EligibilityStatus.ELIGIBILITY_UNKNOWN.value,
        }
    )
    unassigned_rejected = sum(
        1
        for event in unassigned_events
        if event.eligibility_status == EligibilityStatus.INELIGIBLE.value
        or event.verification_status == VerificationStatus.REJECTED.value
    )
    if unassigned_events:
        overall_limitations.append(
            f"{len(unassigned_events)} observation(s) could not be associated with a project "
            f"and are excluded from project scores; {unassigned_pending} require review."
        )

    entries: list[RankingEntry] = []
    now = isoformat(utc_now())
    for project in competition.projects:
        project_events = [
            event for event in active_events if event.project_id == project.project_id
        ]
        project_polls = [poll for poll in active_polls if poll.project_id == project.project_id]
        confirmed_by_method: dict[str, int] = {}
        aggregate_by_method: dict[str, int] = {}
        confidence_breakdown: dict[str, int] = {}
        accounts: set[str] = set()
        confirmed_individual = 0
        provisional = 0
        rejected_late = 0
        pending = 0
        ambiguous = 0
        raw = 0
        (
            aggregate_confirmed,
            aggregate_provisional,
            aggregate_breakdown,
            aggregate_conf,
            aggregate_raw,
        ) = _aggregate_counts(project.project_id, project_polls)

        for event in sorted(project_events, key=lambda item: item.event_id):
            raw += 1
            ambiguous_reason = event.decision_reason.lower().startswith("ambiguous project")
            if ambiguous_reason:
                ambiguous += 1
                continue
            if (
                event.eligibility_status == EligibilityStatus.INELIGIBLE.value
                or event.verification_status == VerificationStatus.REJECTED.value
            ):
                rejected_late += 1
                continue
            if (
                event.eligibility_status == EligibilityStatus.ELIGIBLE.value
                and event.verification_status == VerificationStatus.VERIFIED.value
            ):
                confirmed_individual += 1
                confirmed_by_method[event.voting_method] = (
                    confirmed_by_method.get(event.voting_method, 0) + 1
                )
                if event.account_id:
                    accounts.add(event.account_id)
                confidence_breakdown[event.confidence_label] = (
                    confidence_breakdown.get(event.confidence_label, 0) + 1
                )
                continue
            if event.eligibility_status in {
                EligibilityStatus.ELIGIBLE.value,
                EligibilityStatus.PENDING_ELIGIBILITY_REVIEW.value,
                EligibilityStatus.ELIGIBILITY_UNKNOWN.value,
            }:
                if event.verification_status in UNRESOLVED_VERIFICATION:
                    provisional += 1
                    if event.verification_status in {
                        VerificationStatus.PENDING_REVIEW.value,
                        VerificationStatus.DISPUTED.value,
                    }:
                        pending += 1
                elif event.verification_status == VerificationStatus.INCONCLUSIVE.value:
                    provisional += 1
                    pending += 1
            elif event.eligibility_status == EligibilityStatus.PENDING_ELIGIBILITY_REVIEW.value:
                provisional += 1
                pending += 1

        confirmed_votes = confirmed_individual + aggregate_confirmed
        provisional_votes = provisional + aggregate_provisional
        raw += aggregate_raw
        confidence_breakdown.update(aggregate_conf)
        aggregate_by_method.update(aggregate_breakdown)
        entry = RankingEntry(
            rank=0,
            project_id=project.project_id,
            name=project.name,
            external_id=project.external_id,
            confirmed_votes=confirmed_votes,
            confirmed_individual=confirmed_individual,
            confirmed_aggregate=aggregate_confirmed,
            raw_activity=raw,
            provisional_votes=provisional_votes,
            rejected_or_late_activity=rejected_late,
            pending_review=pending,
            ambiguous=ambiguous,
            breakdown=confirmed_by_method,
            aggregate_breakdown=aggregate_by_method,
            confidence_breakdown=confidence_breakdown,
            account_count=len(accounts),
            coverage_status=overall_coverage,
            last_calculated=now,
            finalization_status=competition.status,
            limitations=_entry_limitations(competition, coverage),
        )
        entry.score, _, _ = _score_entry(
            competition,
            project,
            entry,
            confirmed_by_method,
            aggregate_by_method,
            accounts,
            combined_allowed=combined_allowed,
        )
        entry.unresolved_count = entry.pending_review + entry.ambiguous
        entries.append(entry)

    if view == "provisional":
        for entry in entries:
            entry.score = float(entry.provisional_votes)
    elif view == "raw":
        for entry in entries:
            entry.score = float(entry.raw_activity)
    elif view == "account":
        for entry in entries:
            entry.score = float(entry.account_count)
        overall_limitations.append(
            "Account view counts observed X accounts, never unique humans; "
            "anonymous poll votes are excluded."
        )
    elif view == "weighted":
        for entry in entries:
            weights = {
                key: competition.ranking.weights.get(key, 1.0)
                for key in set(entry.breakdown) | set(entry.aggregate_breakdown)
            }
            if not weights:
                weights = {record: 1.0 for record in competition.methods}
            entry.score = round(
                sum(entry.breakdown.get(method, 0) * weight for method, weight in weights.items())
                + sum(
                    entry.aggregate_breakdown.get(method, 0) * weight
                    for method, weight in weights.items()
                ),
                6,
            )
        overall_limitations.append(
            "Weighted scores combine unlike methods by explicit operator weights; "
            "original counts are preserved."
        )

    entries.sort(key=lambda entry: (-entry.score, entry.name.casefold(), entry.project_id))
    previous_score: float | None = None
    previous_rank = 0
    for index, entry in enumerate(entries, start=1):
        if previous_score is not None and abs(entry.score - previous_score) < 1e-9:
            entry.rank = previous_rank
        else:
            entry.rank = index
            previous_rank = index
            previous_score = entry.score

    denominator: int | None = None
    denominator_kind = ""
    if view in {"confirmed", "provisional", "account"}:

        def _share_value(entry: RankingEntry) -> int:
            if view == "provisional":
                return entry.provisional_votes
            if view == "account":
                return entry.account_count
            return entry.confirmed_votes if combined_allowed else entry.confirmed_individual

        total = sum(_share_value(entry) for entry in entries)
        if total > 0:
            denominator = total
            denominator_kind = {
                "confirmed": (
                    "confirmed votes, including operator-approved source-reported aggregates"
                    if combined_allowed
                    else "confirmed individual votes (source-reported aggregates excluded)"
                ),
                "provisional": "provisional votes (unresolved activity, not confirmed)",
                "account": (
                    "observed account IDs (not unique humans; anonymous poll votes excluded)"
                ),
            }[view]
            for entry in entries:
                entry.vote_share = round(_share_value(entry) / total * 100, 2)
                entry.vote_share_denominator = total

    metric = ""
    metric_description = ""
    score_label = ""
    if view == "confirmed":
        metric = "confirmed_votes"
        metric_description = (
            "Confirmed eligible votes under the configured ruleset for the selected metric."
        )
        score_label = (
            "confirmed votes (individual events + operator-approved aggregates)"
            if combined_allowed
            else "confirmed individual votes (aggregates reported separately)"
        )
    elif view == "provisional":
        metric = "provisional_votes"
        metric_description = "Unresolved activity pending review; not confirmed and not final."
        score_label = "provisional votes (not confirmed)"
    elif view == "raw":
        metric = "raw_observed_activity"
        metric_description = (
            "Every observed activity item, including rejected and unresolved items."
        )
        score_label = "raw observed activity (never valid votes)"
    elif view == "account":
        metric = "observed_accounts"
        metric_description = "Distinct observed account IDs among confirmed individual events."
        score_label = "observed account IDs (not unique humans)"
    elif view == "weighted":
        metric = "weighted_score"
        metric_description = "Operator-configured weighted combination of method counts."
        score_label = "weighted score (not a vote count)"
    elif view == "method":
        metric = "confirmed_votes_by_method"
        metric_description = "Confirmed votes shown per method in the breakdown columns."
        score_label = "confirmed votes (see per-method breakdown)"
    else:
        metric = "timeline"
        metric_description = "Observation timeline for the selected competition."
        score_label = "confirmed votes for reference"

    mode = competition.ranking.mode
    if view == "weighted":
        weight_bits = ", ".join(
            f"{key}={value:g}" for key, value in sorted(competition.ranking.weights.items())
        )
        formula = (
            "weighted = Σ(method count × weight); unconfigured methods default to weight 1.0. "
            + (
                f"Configured weights: {weight_bits}"
                if weight_bits
                else "No explicit weights configured."
            )
        )
    elif mode == "weighted":
        formula = (
            "weighted = Σ(confirmed method count × weight); missing weights default to 1.0. "
            "Weights: "
            + ", ".join(
                f"{key}={value:g}" for key, value in sorted(competition.ranking.weights.items())
            )
        )
    elif mode == "custom":
        formula = f"custom = {competition.ranking.custom_formula}"
    elif mode == "unique_account":
        formula = (
            "unique account score = distinct observed account IDs among confirmed individual events"
        )
    elif mode == "separate":
        formula = (
            "separate counts only: ranking uses confirmed individual events; aggregate totals are "
            "reported separately and never merged into the score"
        )
    elif combined_allowed:
        formula = (
            "confirmed = confirmed individual events + confirmed source-reported aggregate votes "
            "(operator explicitly approved this combination)"
        )
    else:
        formula = (
            "confirmed individual events only; source-reported aggregate votes "
            "(poll/manual/external/OCR) are reported separately and excluded from the score"
        )

    sources_payload = []
    for source in competition.sources:
        cov = coverage.get(source.source_id)
        sources_payload.append(
            {
                "source_id": source.source_id,
                "label": source.label,
                "source_type": source.source_type,
                "url": source.url,
                "post_id": source.post_id or source.fixture_ref,
                "methods": list(source.methods or competition.methods),
                "collection_status": cov.collection_status
                if cov
                else CollectionStatus.NOT_STARTED.value,
                "individual_voters_identifiable": bool(cov.individual_voters_identifiable)
                if cov
                else False,
                "exact_counts_available": bool(cov.exact_counts_available) if cov else False,
                "partial": bool(cov.partial) if cov else False,
                "manually_supplied": bool(cov.manually_supplied) if cov else False,
                "last_checked": cov.last_checked if cov else "",
                "unavailable_methods": list(cov.unavailable_methods) if cov else [],
            }
        )

    if competition.sources:
        overall_limitations.append(
            "Leaderboard rankings are only as complete as the disclosed source coverage."
        )
    if overall_coverage != CollectionStatus.COMPLETE_FOR_SCOPE.value:
        overall_limitations.append(
            f"Collection status is {overall_coverage}; results are not complete "
            "for all configured methods."
        )
    return Leaderboard(
        competition_id=competition.competition_id,
        competition_name=competition.name,
        view=view,
        mode=mode,
        generated_at=now,
        ruleset_version=competition.ruleset_version,
        entries=entries,
        total_confirmed=sum(entry.confirmed_votes for entry in entries),
        denominator=denominator,
        coverage_status=overall_coverage,
        limitations=list(dict.fromkeys(overall_limitations)),
        finalization_status=competition.status,
        formula=formula,
        is_test_data=is_test_data,
        metric=metric,
        metric_description=metric_description,
        score_label=score_label,
        denominator_kind=denominator_kind,
        confirmed_individual_total=sum(entry.confirmed_individual for entry in entries),
        confirmed_aggregate_total=sum(entry.confirmed_aggregate for entry in entries),
        total_unresolved=sum(entry.unresolved_count for entry in entries) + unassigned_pending,
        unassigned_events=len(unassigned_events),
        unassigned_pending_review=unassigned_pending,
        unassigned_rejected=unassigned_rejected,
        aggregates_ranked_separately=not combined_allowed,
        sources=sources_payload,
    )


def create_snapshot(
    competition: Competition,
    leaderboard: Leaderboard,
    *,
    collection_status: str,
    view: str = "confirmed",
) -> Snapshot:
    snapshot = Snapshot(
        snapshot_id="snp_"
        + stable_hash(
            competition.competition_id,
            leaderboard.generated_at,
            leaderboard.ruleset_version,
            view,
            utc_now().isoformat(timespec="microseconds"),
            length=16,
        ),
        competition_id=competition.competition_id,
        created_at=isoformat(utc_now()),
        ruleset_version=competition.ruleset_version,
        view=view,
        ranks={entry.project_id: entry.rank for entry in leaderboard.entries},
        counts={entry.project_id: int(entry.score) for entry in leaderboard.entries},
        coverage_status=leaderboard.coverage_status,
        collection_status=collection_status,
    )
    return snapshot


def compare_snapshots(previous: Snapshot, current: Snapshot) -> dict[str, Any]:
    rank_changes: dict[str, Any] = {}
    count_changes: dict[str, Any] = {}
    for project_id in sorted(set(previous.ranks) | set(current.ranks)):
        old_rank = previous.ranks.get(project_id)
        new_rank = current.ranks.get(project_id)
        if old_rank != new_rank:
            rank_changes[project_id] = {"from": old_rank, "to": new_rank}
        old_count = previous.counts.get(project_id, 0)
        new_count = current.counts.get(project_id, 0)
        if old_count != new_count:
            count_changes[project_id] = {
                "from": old_count,
                "to": new_count,
                "delta": new_count - old_count,
            }
    return {
        "from": previous.created_at,
        "to": current.created_at,
        "rank_changes": rank_changes,
        "count_changes": count_changes,
        "ruleset_changed": previous.ruleset_version != current.ruleset_version,
        "coverage_changed": previous.coverage_status != current.coverage_status,
    }


def build_timeline(
    competition: Competition,
    events: list[NormalizedEvent],
    polls: list[PollAggregate],
    snapshots: list[Snapshot],
) -> dict[str, Any]:
    timeline: list[dict[str, Any]] = []
    collections: dict[tuple[str, str], list[str]] = {}
    for event in events:
        if event.competition_id != competition.competition_id:
            continue
        collections.setdefault((event.source_id, event.source_tool), []).append(event.collected_at)
        if event.manual_decision:
            timeline.append(
                {
                    "type": "verification_decision",
                    "timestamp": event.collected_at,
                    "source_id": event.source_id,
                    "event_id": event.event_id,
                    "summary": event.decision_reason,
                    "verification_status": event.verification_status,
                }
            )
    for (source_id, tool), timestamps in sorted(collections.items()):
        ordered = sorted(timestamps)
        timeline.append(
            {
                "type": "source_observation",
                "timestamp": ordered[0],
                "source_id": source_id,
                "source_tool": tool,
                "observations": len(ordered),
                "summary": f"{len(ordered)} observations collected via {tool}.",
            }
        )
        parsed = []
        for value in ordered:
            try:
                parsed.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                continue
        for left, right in zip(parsed, parsed[1:], strict=False):
            gap = right - left
            if gap >= timedelta(hours=6):
                timeline.append(
                    {
                        "type": "collection_gap",
                        "timestamp": isoformat(left + gap / 2),
                        "source_id": source_id,
                        "gap_hours": round(gap.total_seconds() / 3600, 2),
                        "summary": (
                            "No observations collected during this interval; it is a "
                            "collection gap, not proof of inactivity."
                        ),
                    }
                )
    for poll in polls:
        if poll.competition_id == competition.competition_id:
            timeline.append(
                {
                    "type": "poll_observation",
                    "timestamp": poll.collected_at,
                    "source_id": poll.source_id,
                    "summary": (
                        f"Poll option '{poll.option_label}' observed with "
                        f"{poll.votes} aggregate votes."
                    ),
                    "verification_status": poll.verification_status,
                }
            )
    for snapshot in snapshots:
        if snapshot.competition_id == competition.competition_id:
            timeline.append(
                {
                    "type": "snapshot",
                    "timestamp": snapshot.created_at,
                    "snapshot_id": snapshot.snapshot_id,
                    "summary": "Ranking snapshot captured.",
                    "ranks": snapshot.ranks,
                    "counts": snapshot.counts,
                }
            )
    timeline.sort(key=lambda item: (item["timestamp"], item["type"]))
    return {
        "competition_id": competition.competition_id,
        "events": timeline,
        "limitations": [
            "Source edits and deletions are not independently observable through "
            "the configured adapters; only collected observations appear.",
            "Gaps between observations are collection gaps, not evidence that voting stopped.",
        ],
    }
