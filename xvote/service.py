"""Application service: sessions, workflow, collection, review, ranking, export."""

from __future__ import annotations

import asyncio
import base64
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .adapters.base import (
    AcquisitionAdapter,
    CollectionError,
    SourceUnavailableError,
)
from .adapters.manual import (
    ImportValidationError,
    ManualImportAdapter,
    OCREvidenceAdapter,
    OCRUnavailable,
)
from .adapters.router import AcquisitionRouter
from .adapters.scweet_adapter import ScweetAdapter
from .adapters.synthetic import SyntheticFixtureAdapter, ensure_fixture_shape
from .adapters.twscrape_adapter import TwscrapeAdapter
from .adapters.xcli import XCliAdapter
from .adapters.xtweetfetcher import XTweetFetcherAdapter
from .agents.providers import ModelProvider, build_provider
from .agents.runtime import AgentRuntime
from .config import AcquisitionConfig, load_acquisition_config
from .eligibility import (
    ConfigurationError,
    apply_account_constraints,
    apply_manual_poll_review,
    apply_manual_review,
    competition_window,
    deduplicate_against_existing,
    evaluate_event,
    evaluate_poll,
)
from .export import ExportError, available_fields, build_export, write_export
from .fixtures import DEMO_FIXTURE, FIXTURE_LABEL
from .models import (
    AGGREGATE_METHODS,
    AuditRecord,
    CollectionStatus,
    Competition,
    Leaderboard,
    NormalizedEvent,
    PollAggregate,
    Project,
    RankingRules,
    Snapshot,
    Source,
    SourceCoverage,
    SourceType,
    VerificationStatus,
    VotingMethod,
    isoformat,
    parse_timestamp,
    stable_hash,
    utc_now,
)
from .normalization import choose_method, normalize_account_event, normalize_manual, normalize_reply
from .ranking import (
    FormulaError,
    build_leaderboard,
    build_timeline,
    compare_snapshots,
    create_snapshot,
    validate_formula,
)
from .storage import MemoryEventStore, Session, SessionManager


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class AppService:
    def __init__(
        self,
        config: AcquisitionConfig | None = None,
        *,
        manager: SessionManager | None = None,
        provider: ModelProvider | None = None,
        export_root: Path | str | None = None,
    ) -> None:
        self.config = config or load_acquisition_config()
        self.sessions = manager or SessionManager()
        self.provider = provider or build_provider(self.config)
        env_export = os.environ.get("XVOTE_EXPORT_DIR", "").strip()
        self.export_root = (
            Path(export_root)
            if export_root
            else (Path(env_export) if env_export else Path.home() / ".x-vote" / "exports")
        )
        self._runtimes: dict[str, AgentRuntime] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- sessions

    def create_session(
        self,
        *,
        storage_mode: str = "auto",
        fixture_mode: bool = False,
        fixture: dict[str, Any] | None = None,
    ) -> Session:
        session = self.sessions.create(storage_mode=storage_mode, fixture_mode=fixture_mode)
        if fixture:
            ensure_fixture_shape(fixture)
            session.fixture = fixture
        session.checkpoint()
        return session

    def require_session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if not session:
            raise ServiceError(
                "session not found or already cleaned up", status=404, code="session_not_found"
            )
        return session

    def delete_session(self, session_id: str) -> dict[str, Any]:
        self.cancel_agents(session_id)
        ok, errors = self.sessions.delete(session_id)
        if not ok and errors == ["session not found"]:
            raise ServiceError("session not found", status=404, code="session_not_found")
        return {
            "ok": ok,
            "errors": errors,
            "note": "Exports created earlier are outside session cleanup and remain on disk.",
        }

    def cleanup_all(self) -> dict[str, Any]:
        self.cancel_all_agents()
        return self.sessions.cleanup_all()

    # --------------------------------------------------------- competitions

    @staticmethod
    def _localize_window(competition: Competition) -> None:
        """Attach the configured timezone to naive operator-supplied timestamps."""

        def localize(value: str) -> str:
            if not value:
                return value
            try:
                parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ServiceError(
                    f"invalid ISO-8601 timestamp: {value!r}",
                    status=422,
                    code="invalid_configuration",
                ) from exc
            if parsed.tzinfo is None:
                try:
                    zone = ZoneInfo(competition.timezone_name)
                except Exception as exc:
                    raise ServiceError(
                        f"unknown timezone: {competition.timezone_name!r}",
                        status=422,
                        code="invalid_configuration",
                    ) from exc
                parsed = parsed.replace(tzinfo=zone)
            return parsed.isoformat()

        updated_start = localize(competition.starts_at)
        updated_end = localize(competition.ends_at)
        if updated_start != competition.starts_at or updated_end != competition.ends_at:
            competition.starts_at = updated_start
            competition.ends_at = updated_end
            competition.refresh_ruleset()

    def validate_competition(self, competition: Competition) -> None:
        self._localize_window(competition)
        try:
            competition_window(competition)
        except ConfigurationError as exc:
            raise ServiceError(str(exc), status=422, code="invalid_configuration") from exc
        if not competition.starts_at or not competition.ends_at:
            raise ServiceError(
                "competition start and end timestamps with explicit timezones are required",
                status=422,
                code="invalid_configuration",
            )
        if not competition.projects:
            raise ServiceError(
                "at least one project is required", status=422, code="invalid_configuration"
            )
        if not competition.methods:
            raise ServiceError(
                "at least one voting method is required", status=422, code="invalid_configuration"
            )
        if not competition.sources:
            raise ServiceError(
                "at least one source is required", status=422, code="invalid_configuration"
            )
        for source in competition.sources:
            if source.project_id and not competition.project_by_id(source.project_id):
                raise ServiceError(
                    f"source {source.source_id} references an unknown project",
                    status=422,
                    code="invalid_configuration",
                )
            if source.source_type == SourceType.FIXTURE.value and not (
                source.fixture_ref or source.post_id
            ):
                raise ServiceError(
                    f"fixture source {source.source_id} needs a fixture_ref",
                    status=422,
                    code="invalid_configuration",
                )
        if competition.ranking.mode == "custom":
            try:
                validate_formula(competition.ranking.custom_formula, competition.methods)
            except FormulaError as exc:
                raise ServiceError(
                    f"invalid custom ranking formula: {exc}",
                    status=422,
                    code="invalid_configuration",
                ) from exc

    def create_competition(
        self, session: Session, data: dict[str, Any], *, confirmed: bool
    ) -> Competition:
        if not confirmed:
            raise ServiceError(
                "competition configuration must be explicitly confirmed before activation",
                status=428,
                code="confirmation_required",
            )
        try:
            competition = Competition.from_dict(data)
        except ValueError as exc:
            raise ServiceError(str(exc), status=422, code="invalid_configuration") from exc
        self.validate_competition(competition)
        with session.lock:
            session.add_competition(competition)
            self._audit(
                session,
                competition,
                actor="operator",
                action="competition_created",
                note=f"Ruleset {competition.ruleset_version}",
            )
            session.checkpoint()
        return competition

    def update_competition(
        self, session: Session, competition_id: str, patch: dict[str, Any]
    ) -> Competition:
        competition = self.require_competition(session, competition_id)
        merged = competition.to_dict()
        for key, value in patch.items():
            if key in {"competition_id", "created_at", "ruleset_version"}:
                continue
            merged[key] = value
        try:
            updated = Competition.from_dict(merged)
        except ValueError as exc:
            raise ServiceError(str(exc), status=422, code="invalid_configuration") from exc
        updated.refresh_ruleset()
        self.validate_competition(updated)
        with session.lock:
            session.competitions[competition_id] = updated
            self._reapply_ruleset(session, updated)
            self._audit(
                session,
                updated,
                actor="operator",
                action="ruleset_updated",
                note=(
                    f"New ruleset {updated.ruleset_version}; "
                    "events re-evaluated without recollection."
                ),
            )
            session.checkpoint()
        return updated

    def set_competition_status(
        self, session: Session, competition_id: str, status: str
    ) -> Competition:
        valid = {"collecting", "provisional", "finalized"}
        if status not in valid:
            raise ServiceError(
                "status must be collecting, provisional or finalized",
                status=422,
                code="invalid_status",
            )
        competition = self.require_competition(session, competition_id)
        with session.lock:
            before = {"status": competition.status}
            competition.status = status
            competition.updated_at = isoformat(utc_now())
            self._audit(
                session,
                competition,
                actor="operator",
                action="competition_status_changed",
                before=before,
                after={"status": status},
                note=f"Result status changed to {status}.",
            )
            session.checkpoint()
        return competition

    def require_competition(self, session: Session, competition_id: str) -> Competition:
        competition = session.competitions.get(competition_id)
        if not competition:
            raise ServiceError("competition not found", status=404, code="competition_not_found")
        return competition

    def _audit(
        self,
        session: Session,
        competition: Competition,
        *,
        actor: str,
        action: str,
        event_id: str = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        note: str = "",
    ) -> AuditRecord:
        record = AuditRecord(
            audit_id="aud_"
            + stable_hash(
                competition.competition_id, action, event_id, isoformat(utc_now()), length=16
            ),
            timestamp=isoformat(utc_now()),
            actor=actor,
            action=action,
            competition_id=competition.competition_id,
            event_id=event_id,
            before=before or {},
            after=after or {},
            note=note,
        )
        session.store.add_audit(record)
        return record

    def _reapply_ruleset(self, session: Session, competition: Competition) -> None:
        all_events = session.store.list_events(competition.competition_id)
        for event in all_events:
            if event.superseded_by:
                event.ruleset_version = competition.ruleset_version
                session.store.add_event(event)
        events = [event for event in all_events if not event.superseded_by]
        for event in events:
            if event.manual_decision:
                event.ruleset_version = competition.ruleset_version
                session.store.add_event(event)
                continue
            source = competition.source_by_id(event.source_id)
            text = str(event.normalized_value.get("text_excerpt") or "")
            if source and text and event.voting_method not in AGGREGATE_METHODS:
                event.voting_method = choose_method(
                    competition,
                    source,
                    text,
                    is_quote=bool(event.normalized_value.get("is_quote")),
                )
            event.normalized_value.pop("_pre_account", None)
            evaluate_event(competition, event)
            event.ruleset_version = competition.ruleset_version
            session.store.add_event(event)
        apply_account_constraints(competition, events)
        for event in events:
            session.store.add_event(event)
        polls = session.store.list_polls(competition.competition_id)
        for poll in polls:
            if poll.manual_decision:
                poll.ruleset_version = competition.ruleset_version
                session.store.add_poll(poll)
                continue
            evaluate_poll(competition, poll, match_option=poll.option_label)
            poll.ruleset_version = competition.ruleset_version
            session.store.add_poll(poll)

    # ------------------------------------------------------------ collection

    def _build_router(
        self, session: Session, competition: Competition | None = None
    ) -> AcquisitionRouter:
        adapters: dict[str, AcquisitionAdapter] = {}
        if self.config.x_tweet_fetcher.enabled:
            adapters["x-tweet-fetcher"] = XTweetFetcherAdapter(self.config.x_tweet_fetcher)
        if self.config.x_cli.enabled:
            adapters["x-cli"] = XCliAdapter(self.config.x_cli, session.paths.directory)
        if self.config.twscrape.enabled:
            adapters["twscrape"] = TwscrapeAdapter(self.config.twscrape, session.paths.directory)
        if self.config.scweet.enabled:
            adapters["Scweet"] = ScweetAdapter(self.config.scweet)
        order = list(
            competition.collection.adapter_preference
            if competition and competition.collection.adapter_preference
            else self.config.preference_order
        )
        if session.fixture_mode:
            fixture = session.fixture or DEMO_FIXTURE
            ensure_fixture_shape(fixture)
            adapters["synthetic-fixture"] = SyntheticFixtureAdapter(fixture, enabled=True)
            order = ["synthetic-fixture"] + [name for name in order if name != "synthetic-fixture"]
        return AcquisitionRouter(
            adapters, order, timeout_seconds=self.config.per_task_timeout_seconds
        )

    def adapter_matrix(self) -> list[dict[str, Any]]:
        session = self.sessions.create(storage_mode="memory")
        try:
            router = self._build_router(session)
            return router.capability_matrix()
        finally:
            self.sessions.delete(session.session_id)

    async def _collect_source(
        self,
        router: AcquisitionRouter,
        competition: Competition,
        source: Source,
        methods: list[str],
    ) -> dict[str, Any]:
        outcome: dict[str, Any] = {
            "source": source,
            "post": None,
            "post_failure": "",
            "replies": [],
            "likes": [],
            "reposts": [],
            "poll": None,
            "failures": [],
            "attempts": [],
            "succeeded": [],
            "limitations": [],
        }
        post_id = source.post_id or source.fixture_ref
        live_source_types = {
            SourceType.X_POST.value,
            SourceType.POLL.value,
            SourceType.REPLIES.value,
            SourceType.LIKES.value,
            SourceType.REPOSTS.value,
            SourceType.QUOTES.value,
            SourceType.MENTIONS.value,
            SourceType.HASHTAGS.value,
            SourceType.KEYWORDS.value,
            SourceType.FIXTURE.value,
        }
        if source.source_type in live_source_types:
            if post_id:
                try:
                    result = await router.fetch_tweet(post_id)
                    outcome["post"] = dict(result.value or {})
                    outcome["attempts"].extend(result.attempts)
                    if not outcome["post"]:
                        outcome["post_failure"] = "source post was unavailable"
                except (CollectionError, SourceUnavailableError) as exc:
                    outcome["post_failure"] = str(exc)
            requested = [
                method
                for method in methods
                if method
                in {
                    VotingMethod.REPLY.value,
                    VotingMethod.QUOTE.value,
                    VotingMethod.HASHTAG.value,
                    VotingMethod.KEYWORD.value,
                    VotingMethod.MENTION.value,
                }
            ]
            if requested:
                try:
                    result = await router.fetch_replies(post_id, -1)
                    outcome["replies"] = list(result.value)
                    outcome["attempts"].extend(result.attempts)
                    outcome["succeeded"].append(VotingMethod.REPLY.value)
                    outcome["limitations"].extend(result.limitations)
                    if VotingMethod.QUOTE.value in requested:
                        outcome["limitations"].append(
                            "Quote-post discovery is limited to quote markers "
                            "exposed in reply listings; "
                            "quote-only activity may be incomplete."
                        )
                except (CollectionError, SourceUnavailableError) as exc:
                    outcome["failures"].append(
                        {"method": VotingMethod.REPLY.value, "error": str(exc)}
                    )
            if VotingMethod.LIKE.value in methods:
                try:
                    result = await router.fetch_likers(post_id, -1)
                    outcome["likes"] = list(result.value)
                    outcome["attempts"].extend(result.attempts)
                    outcome["succeeded"].append(VotingMethod.LIKE.value)
                except (CollectionError, SourceUnavailableError) as exc:
                    outcome["failures"].append(
                        {"method": VotingMethod.LIKE.value, "error": str(exc)}
                    )
            if VotingMethod.REPOST.value in methods:
                try:
                    result = await router.fetch_reposters(post_id, -1)
                    outcome["reposts"] = list(result.value)
                    outcome["attempts"].extend(result.attempts)
                    outcome["succeeded"].append(VotingMethod.REPOST.value)
                except (CollectionError, SourceUnavailableError) as exc:
                    outcome["failures"].append(
                        {"method": VotingMethod.REPOST.value, "error": str(exc)}
                    )
            if VotingMethod.NATIVE_POLL.value in methods:
                try:
                    result = await router.fetch_poll(post_id)
                    outcome["poll"] = result.value
                    outcome["attempts"].extend(result.attempts)
                    outcome["succeeded"].append(VotingMethod.NATIVE_POLL.value)
                    outcome["limitations"].extend(result.limitations)
                except (CollectionError, SourceUnavailableError) as exc:
                    outcome["failures"].append(
                        {"method": VotingMethod.NATIVE_POLL.value, "error": str(exc)}
                    )
        else:
            outcome["limitations"].append(
                f"Source type '{source.source_type}' is supplied through manual "
                "import, not live acquisition."
            )
        for method in methods:
            if (
                method
                in {
                    VotingMethod.MANUAL_TOTAL.value,
                    VotingMethod.EXTERNAL_AGGREGATE.value,
                    VotingMethod.OCR_TOTAL.value,
                    VotingMethod.CUSTOM.value,
                }
                and method not in outcome["succeeded"]
            ):
                outcome["limitations"].append(
                    f"Method '{method}' requires manual import or review; "
                    "it was not collected live."
                )
        return outcome

    def collect(
        self,
        session: Session,
        competition_id: str,
        *,
        methods: list[str] | None = None,
        run_agents: bool = True,
    ) -> dict[str, Any]:
        competition = self.require_competition(session, competition_id)
        if competition.status == "finalized":
            raise ServiceError(
                "competition is finalized; reopen it before collecting more data",
                status=409,
                code="competition_finalized",
            )
        live = not session.fixture_mode and any(
            source.source_type
            in {
                SourceType.X_POST.value,
                SourceType.POLL.value,
                SourceType.REPLIES.value,
                SourceType.LIKES.value,
                SourceType.REPOSTS.value,
                SourceType.QUOTES.value,
                SourceType.MENTIONS.value,
                SourceType.HASHTAGS.value,
                SourceType.KEYWORDS.value,
            }
            for source in competition.sources
        )
        if live and not self.config.authorized_use_acknowledged:
            raise ServiceError(
                "Live acquisition requires an explicit authorized-use acknowledgement. "
                f"Set {self.config.authorized_use_env}=yes after confirming that you have "
                "authorized access, will respect the tool licenses, X's terms and access "
                "controls, and will not bypass CAPTCHA, authentication restrictions or rate "
                "limits. Fixture and manual imports do not require this.",
                status=428,
                code="authorized_use_not_acknowledged",
            )
        router = self._build_router(session, competition)

        async def run() -> list[dict[str, Any]]:
            semaphore = asyncio.Semaphore(self.config.max_concurrency)

            async def one(source: Source) -> dict[str, Any]:
                source_methods = methods or source.methods or competition.methods
                async with semaphore:
                    return await self._collect_source(
                        router, competition, source, list(source_methods)
                    )

            return await asyncio.gather(*(one(source) for source in competition.sources))

        outcomes = asyncio.run(run())
        with session.lock:
            report = self._apply_observations(session, competition, outcomes)
            self._audit(
                session,
                competition,
                actor="collection-orchestrator",
                action="collection_run",
                note=report["status"],
            )
            if competition.collection.snapshot_enabled:
                leaderboard = self.leaderboard(session, competition_id)
                snapshot = create_snapshot(
                    competition, leaderboard, collection_status=report["status"]
                )
                session.store.add_snapshot(snapshot)
            session.checkpoint()
            self._maybe_promote(session)
        if run_agents:
            report["agents"] = self.execute_agents(session, competition_id)
        report["leaderboard"] = self.leaderboard(session, competition_id).to_dict()
        return report

    def _maybe_promote(self, session: Session) -> None:
        if (
            isinstance(session.store, MemoryEventStore)
            and session.store.count_events() >= self.config.max_events_in_memory
        ):
            session.promote_to_sqlite()
            session.checkpoint()

    def _apply_observations(
        self, session: Session, competition: Competition, outcomes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        store = session.store
        existing = store.list_events(competition.competition_id)
        incoming: list[NormalizedEvent] = []
        new_polls: list[PollAggregate] = []
        source_reports: list[dict[str, Any]] = []
        overall_statuses: list[str] = []

        for outcome in outcomes:
            source: Source = outcome["source"]
            test_data = source.source_type == SourceType.FIXTURE.value or session.fixture_mode
            for reply in outcome["replies"]:
                incoming.append(normalize_reply(competition, source, reply, is_test_data=test_data))
            for account in outcome["likes"]:
                incoming.append(
                    normalize_account_event(
                        competition,
                        source,
                        account,
                        method=VotingMethod.LIKE.value,
                        is_test_data=test_data,
                    )
                )
            for account in outcome["reposts"]:
                incoming.append(
                    normalize_account_event(
                        competition,
                        source,
                        account,
                        method=VotingMethod.REPOST.value,
                        is_test_data=test_data,
                    )
                )
            poll = outcome["poll"]
            if poll is not None:
                for option in poll.options:
                    aggregate = PollAggregate(
                        aggregate_id="agg_"
                        + stable_hash(
                            competition.competition_id,
                            source.source_id,
                            poll.post_id,
                            option.get("label", ""),
                            length=20,
                        ),
                        competition_id=competition.competition_id,
                        project_id="",
                        source_id=source.source_id,
                        source_url=poll.url or source.url,
                        source_tool=poll.source_tool,
                        auth_tier=poll.auth_tier,
                        option_label=str(option.get("label", "")),
                        votes=int(option.get("votes", 0)),
                        total_votes=int(poll.total_votes),
                        finished=bool(poll.finished),
                        collected_at=poll.collected_at or isoformat(utc_now()),
                        raw_evidence_reference=poll.raw_evidence_reference,
                        confidence_label="high"
                        if poll.source_tool in {"twscrape", "x-cli"}
                        else "moderate",
                        verification_status=VerificationStatus.PENDING_REVIEW.value,
                        decision_reason="",
                        ruleset_version=competition.ruleset_version,
                        manually_supplied=False,
                        is_test_data=test_data,
                        limitations=list(poll.limitations),
                    )
                    evaluate_poll(competition, aggregate, match_option=aggregate.option_label)
                    new_polls.append(aggregate)
            coverage = session.coverage.get(source.source_id) or SourceCoverage(
                source_id=source.source_id,
                source_type=source.source_type,
                label=source.label or source.source_type,
            )
            self._update_coverage(coverage, outcome)
            session.coverage[source.source_id] = coverage
            overall_statuses.append(coverage.collection_status)
            source_reports.append(
                {
                    "source_id": source.source_id,
                    "label": source.label or source.source_type,
                    "status": coverage.collection_status,
                    "collected_methods": list(coverage.collected_methods),
                    "unavailable_methods": list(coverage.unavailable_methods),
                    "failures": outcome["failures"],
                    "limitations": coverage.limitations,
                    "post_author_username": coverage.post_author_username,
                    "post_metrics": dict(coverage.post_metrics),
                }
            )

        evaluated = [evaluate_event(competition, event) for event in incoming]
        to_store, supersession_audit, _ = deduplicate_against_existing(evaluated, existing)
        for event in to_store:
            store.add_event(event)
        for record in supersession_audit:
            store.add_audit(record)
        for poll in new_polls:
            previous = next(
                (
                    item
                    for item in store.list_polls(competition.competition_id)
                    if item.aggregate_id == poll.aggregate_id
                ),
                None,
            )
            if previous and previous.votes != poll.votes and not previous.manual_decision:
                poll.history = previous.history + [
                    {
                        "collected_at": previous.collected_at,
                        "votes": previous.votes,
                        "source_tool": previous.source_tool,
                    }
                ]
                poll.manually_supplied = previous.manually_supplied
                poll.manual_decision = previous.manual_decision
            elif previous and previous.manual_decision:
                continue
            store.add_poll(poll)
        all_events = store.list_events(competition.competition_id)
        apply_account_constraints(competition, all_events)
        for event in all_events:
            store.add_event(event)
        session.last_collection_at = isoformat(utc_now())
        if not outcomes:
            status = CollectionStatus.NOT_STARTED.value
        elif all(item == CollectionStatus.COMPLETE_FOR_SCOPE.value for item in overall_statuses):
            status = CollectionStatus.COMPLETE_FOR_SCOPE.value
        elif any(
            item
            in {
                CollectionStatus.FAILED.value,
                CollectionStatus.SOURCE_UNAVAILABLE.value,
                CollectionStatus.RATE_LIMITED.value,
                CollectionStatus.PERMISSION_RESTRICTED.value,
                CollectionStatus.NOT_STARTED.value,
            }
            for item in overall_statuses
        ) and all(item != CollectionStatus.COMPLETE_FOR_SCOPE.value for item in overall_statuses):
            status = CollectionStatus.SOURCE_UNAVAILABLE.value
        else:
            status = CollectionStatus.PARTIALLY_COMPLETE.value
        return {
            "competition_id": competition.competition_id,
            "status": status,
            "sources": source_reports,
            "events_observed": sum(
                1
                for event in store.list_events(competition.competition_id)
                if not event.superseded_by
            ),
            "poll_aggregates": len(store.list_polls(competition.competition_id)),
            "limitations": [
                "Collection status covers only the configured sources and methods.",
                "Unavailable methods and failed adapters are listed per source.",
            ],
        }

    def _update_coverage(self, coverage: SourceCoverage, outcome: dict[str, Any]) -> None:
        coverage.last_checked = isoformat(utc_now())
        post = outcome.get("post") or {}
        if post:
            metrics: dict[str, int] = {}
            for key, value in (post.get("metrics") or {}).items():
                try:
                    metrics[str(key)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
            coverage.post_author_username = str(
                post.get("author_username") or post.get("author_id") or ""
            )
            coverage.post_url = str(post.get("url") or "")
            coverage.post_created_at = str(post.get("created_at") or "")
            coverage.post_metrics = metrics
            coverage.post_metrics_collected_at = isoformat(utc_now())
            if metrics:
                coverage.limitations = sorted(
                    set(coverage.limitations)
                    | {
                        "Post-level engagement metrics are context only and are never "
                        "converted into votes."
                    }
                )
        if outcome.get("post_failure"):
            coverage.limitations = sorted(
                set(coverage.limitations)
                | {f"Post metadata unavailable: {outcome['post_failure']}"}
            )
        coverage.collected_methods = sorted(
            set(coverage.collected_methods) | set(outcome["succeeded"])
        )
        coverage.unavailable_methods = sorted(
            set(coverage.unavailable_methods)
            | {failure["method"] for failure in outcome["failures"]}
        )
        coverage.individual_voters_identifiable = bool(
            outcome["replies"] or outcome["likes"] or outcome["reposts"]
        )
        coverage.exact_counts_available = bool(outcome["poll"] is not None or outcome["replies"])
        coverage.partial = bool(outcome["failures"]) or bool(outcome["limitations"])
        coverage.limitations = sorted(set(coverage.limitations) | set(outcome["limitations"]))
        coverage.adapter_attempts = coverage.adapter_attempts + [
            attempt.to_dict() if hasattr(attempt, "to_dict") else dict(attempt)
            for attempt in outcome["attempts"]
        ]
        if outcome["succeeded"] and not outcome["failures"]:
            coverage.collection_status = CollectionStatus.COMPLETE_FOR_SCOPE.value
        elif outcome["succeeded"]:
            coverage.collection_status = CollectionStatus.PARTIALLY_COMPLETE.value
        elif outcome["failures"]:
            messages = " ".join(failure["error"].lower() for failure in outcome["failures"])
            if "rate limit" in messages or "rate_limited" in messages:
                coverage.collection_status = CollectionStatus.RATE_LIMITED.value
            elif "credential" in messages or "session" in messages or "permission" in messages:
                coverage.collection_status = CollectionStatus.PERMISSION_RESTRICTED.value
            else:
                coverage.collection_status = CollectionStatus.SOURCE_UNAVAILABLE.value
        else:
            coverage.collection_status = CollectionStatus.NOT_STARTED.value

    # -------------------------------------------------------------- imports

    def import_manual(
        self,
        session: Session,
        competition_id: str,
        *,
        payload: str,
        format_hint: str = "",
        source_label: str = "manual import",
        source_type: str = "",
        run_agents: bool = False,
    ) -> dict[str, Any]:
        competition = self.require_competition(session, competition_id)
        adapter = ManualImportAdapter()
        try:
            records = adapter.parse(payload, format_hint=format_hint, source_label=source_label)
        except ImportValidationError as exc:
            raise ServiceError(str(exc), status=422, code="invalid_import") from exc
        with session.lock:
            source = self._ensure_manual_source(
                competition, source_label, source_type=source_type or SourceType.MANUAL.value
            )
            counts = self._ingest_manual_records(session, competition, source, records)
            coverage = session.coverage.get(source.source_id)
            if coverage:
                coverage.collection_status = CollectionStatus.MANUALLY_SUPPLIED.value
                coverage.manually_supplied = True
                coverage.last_checked = isoformat(utc_now())
                if any(record.votes is not None for record in records):
                    coverage.exact_counts_available = True
                coverage.individual_voters_identifiable = (
                    coverage.individual_voters_identifiable
                    or any(record.account_id and record.votes is None for record in records)
                )
                coverage.limitations = sorted(
                    set(coverage.limitations)
                    | {
                        "This source is manually supplied; provenance and "
                        "confidence come from the operator.",
                        "Manually supplied aggregates require review before they are confirmed.",
                    }
                )
            self._audit(
                session,
                competition,
                actor="operator",
                action="manual_import",
                note=(
                    f"{counts['events']} event(s), {counts['aggregates']} "
                    f"aggregate(s) from {source_label}"
                ),
            )
            session.checkpoint()
            self._maybe_promote(session)
        result = {
            "competition_id": competition.competition_id,
            **counts,
            "source_id": source.source_id,
        }
        if run_agents:
            result["agents"] = self.execute_agents(session, competition_id, run=run_agents)
        return result

    def _ensure_manual_source(
        self, competition: Competition, label: str, *, source_type: str
    ) -> Source:
        if source_type not in {item.value for item in SourceType}:
            raise ServiceError("unsupported manual source type", status=422, code="invalid_import")
        existing = next(
            (
                source
                for source in competition.sources
                if source.source_type == source_type and (source.label or "") == label
            ),
            None,
        )
        if existing:
            return existing
        if source_type == SourceType.OCR.value:
            methods = [VotingMethod.OCR_TOTAL.value]
        elif source_type == SourceType.EXTERNAL.value:
            methods = [VotingMethod.EXTERNAL_AGGREGATE.value]
        else:
            methods = [VotingMethod.MANUAL_TOTAL.value, VotingMethod.REPLY.value]
        source = Source(
            source_type=source_type,
            label=label or source_type,
            methods=methods,
        )
        competition.sources.append(source)
        return source

    def _ingest_manual_records(
        self,
        session: Session,
        competition: Competition,
        source: Source,
        records: list[Any],
    ) -> dict[str, int]:
        store = session.store
        session.coverage.setdefault(
            source.source_id,
            SourceCoverage(
                source_id=source.source_id,
                source_type=source.source_type,
                label=source.label,
                collection_status=CollectionStatus.MANUALLY_SUPPLIED.value,
                manually_supplied=True,
            ),
        )
        incoming: list[NormalizedEvent] = []
        aggregates: list[PollAggregate] = []
        for record in records:
            if record.votes is not None:
                aggregate = PollAggregate(
                    aggregate_id="agg_"
                    + stable_hash(
                        competition.competition_id,
                        source.source_id,
                        record.record_id,
                        length=20,
                    ),
                    competition_id=competition.competition_id,
                    project_id="",
                    source_id=source.source_id,
                    source_url=record.source_url or source.url,
                    source_tool="manual-import",
                    auth_tier="none",
                    option_label=record.project,
                    votes=int(record.votes),
                    total_votes=int(record.votes),
                    finished=True,
                    collected_at=isoformat(utc_now()),
                    raw_evidence_reference=record.evidence_reference,
                    confidence_label=record.confidence,
                    verification_status=VerificationStatus.PENDING_REVIEW.value,
                    decision_reason="",
                    ruleset_version=competition.ruleset_version,
                    manually_supplied=True,
                    is_test_data=session.fixture_mode,
                    voting_method=record.method,
                    limitations=list(record.limitations),
                )
                evaluate_poll(competition, aggregate, match_option=record.project)
                aggregates.append(aggregate)
            else:
                incoming.append(
                    normalize_manual(competition, source, record, is_test_data=session.fixture_mode)
                )
        evaluated = [evaluate_event(competition, event) for event in incoming]
        existing = store.list_events(competition.competition_id)
        to_store, supersession_audit, _ = deduplicate_against_existing(evaluated, existing)
        for event in to_store:
            store.add_event(event)
        for record in supersession_audit:
            store.add_audit(record)
        for aggregate in aggregates:
            previous = next(
                (
                    item
                    for item in store.list_polls(competition.competition_id)
                    if item.aggregate_id == aggregate.aggregate_id
                ),
                None,
            )
            if previous and previous.manual_decision:
                continue
            if previous:
                aggregate.history = previous.history + [
                    {
                        "collected_at": previous.collected_at,
                        "votes": previous.votes,
                        "source_tool": previous.source_tool,
                    }
                ]
            store.add_poll(aggregate)
        all_events = store.list_events(competition.competition_id)
        apply_account_constraints(competition, all_events)
        for event in all_events:
            store.add_event(event)
        return {"events": len(to_store), "aggregates": len(aggregates)}

    def import_ocr_text(
        self,
        session: Session,
        competition_id: str,
        *,
        text: str,
        confidence: str = "low",
        evidence_reference: str = "",
        source_label: str = "ocr evidence",
    ) -> dict[str, Any]:
        adapter = OCREvidenceAdapter()
        try:
            records = adapter.parse_text(
                text,
                confidence=confidence,
                evidence_reference=evidence_reference,
                source_label=source_label,
            )
        except ImportValidationError as exc:
            raise ServiceError(str(exc), status=422, code="invalid_import") from exc
        return self.import_manual(
            session,
            competition_id,
            payload=json_dumps_records(records),
            format_hint="json",
            source_label=source_label,
            source_type=SourceType.OCR.value,
        )

    def upload_ocr_image(
        self,
        session: Session,
        competition_id: str,
        *,
        image_base64: str,
        confidence: str = "low",
        source_label: str = "ocr upload",
    ) -> dict[str, Any]:
        competition = self.require_competition(session, competition_id)
        adapter = OCREvidenceAdapter()
        try:
            data = base64.b64decode(image_base64, validate=True)
        except Exception as exc:
            raise ServiceError(
                "image payload is not valid base64", status=422, code="invalid_upload"
            ) from exc
        try:
            kind = adapter.validate_image(data)
        except ImportValidationError as exc:
            raise ServiceError(str(exc), status=422, code="invalid_upload") from exc
        digest = stable_hash(source_label, len(data), data[:64].hex(), length=16)
        evidence_path = session.paths.evidence / f"ocr-{digest}.{kind}"
        evidence_path.write_bytes(data)
        try:
            evidence_path.chmod(0o600)
        except OSError:
            pass
        evidence_reference = f"evidence:{evidence_path.name}"
        try:
            text = adapter.extract_text(data)
        except OCRUnavailable as exc:
            raise ServiceError(
                f"{exc}. The image was stored in the session as {evidence_reference}; "
                "paste the OCR text manually to continue.",
                status=422,
                code="ocr_unavailable",
            ) from exc
        result = self.import_ocr_text(
            session,
            competition.competition_id,
            text=text,
            confidence=confidence,
            evidence_reference=evidence_reference,
            source_label=source_label,
        )
        result["evidence_reference"] = evidence_reference
        return result

    # --------------------------------------------------------------- review

    def review_event(
        self,
        session: Session,
        competition_id: str,
        event_id: str,
        *,
        action: str,
        project_id: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        competition = self.require_competition(session, competition_id)
        event = session.store.get_event(event_id)
        if not event or event.competition_id != competition_id:
            raise ServiceError("event not found", status=404, code="event_not_found")
        try:
            audit = apply_manual_review(
                competition, event, action=action, project_id=project_id, note=note
            )
        except ValueError as exc:
            raise ServiceError(str(exc), status=422, code="invalid_review") from exc
        with session.lock:
            session.store.add_event(event)
            session.store.add_audit(audit)
            all_events = session.store.list_events(competition_id)
            apply_account_constraints(competition, all_events)
            for item in all_events:
                session.store.add_event(item)
            session.checkpoint()
        return {"event": event.to_dict(), "audit": audit.to_dict()}

    def bulk_review(
        self,
        session: Session,
        competition_id: str,
        *,
        event_ids: list[str],
        action: str,
        confirm: bool,
        note: str = "",
    ) -> dict[str, Any]:
        if not confirm:
            raise ServiceError(
                "bulk actions require explicit confirmation",
                status=428,
                code="confirmation_required",
            )
        if len(event_ids) > 500:
            raise ServiceError(
                "bulk actions are limited to 500 events", status=422, code="too_many_events"
            )
        results = [
            self.review_event(session, competition_id, event_id, action=action, note=note)
            for event_id in event_ids
        ]
        return {"reviewed": len(results), "results": results}

    def review_poll(
        self,
        session: Session,
        competition_id: str,
        aggregate_id: str,
        *,
        action: str,
        note: str = "",
    ) -> dict[str, Any]:
        self.require_competition(session, competition_id)
        poll = next(
            (
                item
                for item in session.store.list_polls(competition_id)
                if item.aggregate_id == aggregate_id
            ),
            None,
        )
        if not poll:
            raise ServiceError("poll aggregate not found", status=404, code="aggregate_not_found")
        try:
            audit = apply_manual_poll_review(poll, action=action, note=note)
        except ValueError as exc:
            raise ServiceError(str(exc), status=422, code="invalid_review") from exc
        with session.lock:
            session.store.add_poll(poll)
            session.store.add_audit(audit)
            session.checkpoint()
        return {"aggregate": poll.to_dict(), "audit": audit.to_dict()}

    # ------------------------------------------------------------- rankings

    def leaderboard(
        self, session: Session, competition_id: str, view: str = "confirmed"
    ) -> Leaderboard:
        competition = self.require_competition(session, competition_id)
        if view not in {
            "confirmed",
            "provisional",
            "raw",
            "method",
            "account",
            "weighted",
            "timeline",
        }:
            raise ServiceError("unsupported leaderboard view", status=422, code="invalid_view")
        return build_leaderboard(
            competition,
            session.store.list_events(competition_id),
            session.store.list_polls(competition_id),
            session.coverage,
            view=view,
            is_test_data=session.fixture_mode,
        )

    def timeline(self, session: Session, competition_id: str) -> dict[str, Any]:
        competition = self.require_competition(session, competition_id)
        return build_timeline(
            competition,
            session.store.list_events(competition_id),
            session.store.list_polls(competition_id),
            session.store.list_snapshots(competition_id),
        )

    def create_snapshot(self, session: Session, competition_id: str) -> Snapshot:
        competition = self.require_competition(session, competition_id)
        leaderboard = self.leaderboard(session, competition_id)
        snapshot = create_snapshot(
            competition,
            leaderboard,
            collection_status=leaderboard.coverage_status,
        )
        session.store.add_snapshot(snapshot)
        session.checkpoint()
        return snapshot

    def compare_snapshots(
        self, session: Session, competition_id: str, snapshot_id: str
    ) -> dict[str, Any]:
        snapshots = session.store.list_snapshots(competition_id)
        index = next(
            (
                position
                for position, item in enumerate(snapshots)
                if item.snapshot_id == snapshot_id
            ),
            None,
        )
        if index is None:
            raise ServiceError("snapshot not found", status=404, code="snapshot_not_found")
        if index == 0:
            raise ServiceError(
                "no earlier snapshot exists to compare against",
                status=409,
                code="no_previous_snapshot",
            )
        return compare_snapshots(snapshots[index - 1], snapshots[index])

    # --------------------------------------------------------------- agents

    def execute_agents(
        self, session: Session, competition_id: str, *, run: bool = True
    ) -> list[dict[str, Any]]:
        competition = self.require_competition(session, competition_id)
        leaderboard = self.leaderboard(session, competition_id)
        runtime = AgentRuntime(self.config, self.provider)
        with self._lock:
            self._runtimes[session.session_id] = runtime
        try:
            tasks = asyncio.run(
                runtime.run_plan(
                    competition,
                    session.store.list_events(competition_id),
                    session.store.list_polls(competition_id),
                    session.coverage,
                    leaderboard,
                )
            )
        finally:
            with self._lock:
                self._runtimes.pop(session.session_id, None)
        with session.lock:
            session.agent_tasks = [task.to_dict() for task in tasks]
            self._audit(
                session,
                competition,
                actor="agent-orchestrator",
                action="agent_run",
                note=(
                    f"{sum(1 for task in tasks if task.status == 'succeeded')}/"
                    f"{len(tasks)} tasks succeeded"
                ),
            )
            session.checkpoint()
        return session.agent_tasks

    def cancel_agents(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtimes.get(session_id)
        if not runtime:
            return {"cancelled": False, "note": "no agent run is active"}
        runtime.cancel()
        return {
            "cancelled": True,
            "paused": False,
            "note": "cancellation requested; running tasks stop at their next check",
        }

    def cancel_all_agents(self) -> int:
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            runtime.cancel()
        return len(runtimes)

    def set_agents_paused(self, session_id: str, *, paused: bool) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtimes.get(session_id)
        if not runtime:
            return {
                "paused": False,
                "active": False,
                "note": "no agent run is active; pause applies only to a running plan",
            }
        if paused:
            runtime.pause()
        else:
            runtime.resume()
        return {
            "paused": runtime.paused,
            "active": True,
            "note": (
                "New agent work is paused; running tasks finish their current step."
                if paused
                else "New agent work is resumed."
            ),
        }

    # --------------------------------------------------------------- exports

    def export(
        self,
        session: Session,
        competition_id: str,
        *,
        kind: str,
        fmt: str,
        fields: list[str] | None = None,
        confirm_sensitive: bool = False,
        view: str = "confirmed",
    ) -> dict[str, Any]:
        competition = self.require_competition(session, competition_id)
        leaderboard = self.leaderboard(session, competition_id, view=view)
        try:
            package = build_export(
                kind,
                fmt,
                competition=competition,
                leaderboard=leaderboard,
                events=session.store.list_events(competition_id),
                polls=session.store.list_polls(competition_id),
                coverage=session.coverage,
                snapshots=session.store.list_snapshots(competition_id),
                audit=session.store.list_audit(competition_id),
                agent_tasks=session.agent_tasks,
                fields=fields,
                confirm_sensitive=confirm_sensitive,
            )
        except ExportError as exc:
            raise ServiceError(str(exc), status=422, code="invalid_export") from exc
        path = write_export(self.export_root / session.session_id, package)
        record = {**package.to_record(), "path": str(path)}
        with session.lock:
            session.exports.append(record)
            self._audit(
                session,
                competition,
                actor="operator",
                action="export_created",
                note=(
                    f"{package.kind} as {package.format} "
                    f"({'sensitive fields confirmed' if confirm_sensitive else 'standard fields'})"
                ),
            )
            session.checkpoint()
        return record

    def export_fields(self, kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "fields": available_fields(kind),
            "sensitive_fields": sorted(
                {
                    "account_id",
                    "author_username",
                    "normalized_value",
                    "raw_evidence_reference",
                    "text_excerpt",
                    "evidence_excerpt",
                }
            ),
        }

    # -------------------------------------------------------------- overview

    def overview(self, session: Session, competition_id: str | None = None) -> dict[str, Any]:
        if competition_id is None:
            competition_id = session.active_competition_id or next(iter(session.competitions), "")
        if not competition_id or competition_id not in session.competitions:
            return {
                "session_id": session.session_id,
                "created_at": session.created_at,
                "storage_mode": session.storage_mode,
                "competition": None,
                "cleanup_status": session.cleanup_status,
                "fixture_mode": session.fixture_mode,
                "note": "No competition is configured yet.",
            }
        competition = self.require_competition(session, competition_id)
        leaderboard = self.leaderboard(session, competition_id)
        events = [
            event
            for event in session.store.list_events(competition_id)
            if not event.superseded_by and event.ruleset_version == competition.ruleset_version
        ]
        polls = [
            poll
            for poll in session.store.list_polls(competition_id)
            if poll.project_id and poll.ruleset_version == competition.ruleset_version
        ]
        rejected = sum(entry.rejected_or_late_activity for entry in leaderboard.entries)
        pending = sum(entry.pending_review for entry in leaderboard.entries)
        ambiguous = sum(entry.ambiguous for entry in leaderboard.entries)
        created = parse_timestamp(session.created_at)
        duration = int((utc_now() - created).total_seconds()) if created else 0
        return {
            "session_id": session.session_id,
            "created_at": session.created_at,
            "session_duration_seconds": duration,
            "storage_mode": session.storage_mode,
            "fixture_mode": session.fixture_mode,
            "test_data_banner": (
                "SYNTHETIC TEST DATA — NOT LIVE X RESULTS" if session.fixture_mode else ""
            ),
            "competition": {
                "competition_id": competition.competition_id,
                "name": competition.name,
                "status": competition.status,
                "ruleset_version": competition.ruleset_version,
                "starts_at": competition.starts_at,
                "ends_at": competition.ends_at,
                "timezone": competition.timezone_name,
                "methods": list(competition.methods),
                "project_count": len(competition.projects),
                "source_count": len(competition.sources),
            },
            "collection_status": leaderboard.coverage_status,
            "last_collection_at": session.last_collection_at,
            "last_calculated_at": leaderboard.generated_at,
            "total_observed_activity": (
                sum(entry.raw_activity for entry in leaderboard.entries)
                + leaderboard.unassigned_events
            ),
            "total_observed_events": len(events),
            "aggregate_observations": len(polls),
            "confirmed_votes": leaderboard.total_confirmed,
            "confirmed_individual": sum(
                entry.confirmed_individual for entry in leaderboard.entries
            ),
            "confirmed_aggregate": sum(entry.confirmed_aggregate for entry in leaderboard.entries),
            "provisional_votes": sum(entry.provisional_votes for entry in leaderboard.entries),
            "rejected_or_late_activity": rejected + leaderboard.unassigned_rejected,
            "pending_review": pending + leaderboard.unassigned_pending_review,
            "ambiguous": ambiguous,
            "coverage": [cov.to_dict() for cov in session.coverage.values()],
            "limitations": leaderboard.limitations,
            "cleanup_status": session.cleanup_status,
            "exports": list(session.exports),
        }

    def coverage_report(self, session: Session) -> list[dict[str, Any]]:
        return [cov.to_dict() for cov in session.coverage.values()]

    # ---------------------------------------------------------------- demo

    def create_demo_session(self, *, collect: bool = True) -> tuple[Session, dict[str, Any]]:
        session = self.create_session(fixture_mode=True, fixture=DEMO_FIXTURE)
        competition = Competition(
            name="Synthetic demo (TEST DATA)",
            description="Fixture-driven end-to-end demo. All data is synthetic and labelled.",
            timezone_name="Africa/Lagos",
            starts_at="2020-01-01T00:00:00+01:00",
            ends_at="2035-12-31T20:00:00+01:00",
            methods=[
                VotingMethod.NATIVE_POLL.value,
                VotingMethod.REPLY.value,
                VotingMethod.LIKE.value,
                VotingMethod.REPOST.value,
            ],
            ranking=RankingRules(
                mode="confirmed",
                combine_confirmed_with_aggregates=True,
            ),
            projects=[
                Project(name="Alpha"),
                Project(name="Beta", aliases=["Beta"], external_id="S071"),
                Project(name="Gamma"),
            ],
            sources=[
                Source(
                    source_type=SourceType.FIXTURE.value,
                    fixture_ref="900000000000000001",
                    label="Synthetic campaign post",
                    methods=[
                        VotingMethod.NATIVE_POLL.value,
                        VotingMethod.REPLY.value,
                    ],
                ),
                Source(
                    source_type=SourceType.FIXTURE.value,
                    fixture_ref="900000000000000002",
                    label="Synthetic Beta project post",
                    methods=[VotingMethod.LIKE.value, VotingMethod.REPOST.value],
                ),
                Source(
                    source_type=SourceType.FIXTURE.value,
                    fixture_ref="900000000000000003",
                    label="Synthetic Gamma project post",
                    methods=[VotingMethod.LIKE.value, VotingMethod.REPOST.value],
                ),
            ],
        )
        beta = next(project for project in competition.projects if project.name == "Beta")
        gamma = next(project for project in competition.projects if project.name == "Gamma")
        competition.sources[1].project_id = beta.project_id
        competition.sources[2].project_id = gamma.project_id
        competition.collection.snapshot_enabled = True
        competition.refresh_ruleset()
        self.create_competition(session, competition.to_dict(), confirmed=True)
        result: dict[str, Any] = {"competition_id": competition.competition_id}
        if collect:
            result["collection"] = self.collect(
                session, competition.competition_id, run_agents=False
            )
            result["manual_import"] = self.import_manual(
                session,
                competition.competition_id,
                payload=json_dumps_records(
                    [
                        {
                            "project": item["project"],
                            "votes": item["votes"],
                            "method": item["method"],
                            "source_url": item["source_url"],
                            "confidence": item["confidence"],
                            "note": item["note"],
                        }
                        for item in DEMO_FIXTURE.get("manual_totals", [])
                    ]
                ),
                format_hint="json",
                source_label="Synthetic manual totals",
                source_type=SourceType.MANUAL.value,
            )
        result["session_id"] = session.session_id
        result["test_data_banner"] = FIXTURE_LABEL
        result["fixture_mode"] = True
        return session, result


def json_dumps_records(records: list[Any] | list[dict[str, Any]]) -> str:
    import json

    payload = []
    for record in records:
        if hasattr(record, "to_dict"):
            payload.append(record.to_dict())
        elif isinstance(record, dict):
            payload.append(record)
    return json.dumps(payload, ensure_ascii=False)
