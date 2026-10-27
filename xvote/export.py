"""Operator-controlled exports (CSV, JSON, HTML). No auto-upload."""

from __future__ import annotations

import csv
import html
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    AuditRecord,
    CollectionStatus,
    Competition,
    Leaderboard,
    NormalizedEvent,
    PollAggregate,
    Snapshot,
    SourceCoverage,
    stable_hash,
)

SENSITIVE_ACCOUNT_FIELDS = {"account_id", "author_username", "username"}
SENSITIVE_EVIDENCE_FIELDS = {
    "raw_evidence_reference",
    "normalized_value",
    "text_excerpt",
    "evidence_excerpt",
}

LEADERBOARD_DEFAULT_FIELDS = [
    "data_label",
    "test_data",
    "rank",
    "name",
    "external_id",
    "confirmed_votes",
    "confirmed_individual",
    "confirmed_aggregate",
    "provisional_votes",
    "rejected_or_late_activity",
    "pending_review",
    "ambiguous",
    "vote_share",
    "account_count",
    "coverage_status",
    "finalization_status",
    "last_calculated",
    "metric",
    "score_label",
    "denominator_kind",
    "sources",
    "limitations",
]

EVENT_DEFAULT_FIELDS = [
    "event_id",
    "project_id",
    "voting_method",
    "source_id",
    "source_url",
    "source_tool",
    "auth_tier",
    "event_timestamp",
    "collected_at",
    "eligibility_status",
    "verification_status",
    "confidence_label",
    "decision_reason",
    "ruleset_version",
    "review_status",
    "individual_identity_available",
]

COVERAGE_FIELDS = [
    "source_id",
    "source_type",
    "label",
    "collected_methods",
    "unavailable_methods",
    "individual_voters_identifiable",
    "exact_counts_available",
    "partial",
    "manually_supplied",
    "last_checked",
    "collection_status",
    "limitations",
    "adapter_attempts",
]

SNAPSHOT_FIELDS = [
    "snapshot_id",
    "created_at",
    "ruleset_version",
    "view",
    "coverage_status",
    "collection_status",
    "ranks",
    "counts",
]


class ExportError(ValueError):
    pass


@dataclass
class ExportPackage:
    filename: str
    media_type: str
    content: bytes
    kind: str
    format: str
    fields: list[str]
    sensitive_included: list[str] = field(default_factory=list)
    exported_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )

    def to_record(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "kind": self.kind,
            "format": self.format,
            "fields": list(self.fields),
            "sensitive_included": list(self.sensitive_included),
            "exported_at": self.exported_at,
            "size_bytes": len(self.content),
        }


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _csv_safe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _field_value(row: dict[str, Any], field_name: str) -> Any:
    if field_name in row:
        return row[field_name]
    if field_name == "evidence_excerpt":
        return (row.get("normalized_value") or {}).get("text_excerpt", "")
    return ""


def _rows_for_kind(
    kind: str,
    *,
    competition: Competition,
    leaderboard: Leaderboard,
    events: list[NormalizedEvent],
    polls: list[PollAggregate],
    coverage: dict[str, SourceCoverage],
    snapshots: list[Snapshot],
    audit: list[AuditRecord],
    agent_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if kind in {"leaderboard", "provisional", "raw", "method", "account", "weighted"}:
        rows = [entry.to_dict() for entry in leaderboard.entries]
        if kind == "method":
            expanded = []
            for entry in rows:
                for method, count in entry.get("breakdown", {}).items():
                    expanded.append(
                        {
                            "rank": entry["rank"],
                            "name": entry["name"],
                            "project_id": entry["project_id"],
                            "method": method,
                            "confirmed_votes": count,
                            "measure": "individual",
                        }
                    )
                for method, count in entry.get("aggregate_breakdown", {}).items():
                    expanded.append(
                        {
                            "rank": entry["rank"],
                            "name": entry["name"],
                            "project_id": entry["project_id"],
                            "method": method,
                            "confirmed_votes": count,
                            "measure": "anonymous_aggregate",
                        }
                    )
            rows = expanded
        data_label = (
            "SYNTHETIC TEST DATA — NOT LIVE X RESULTS"
            if leaderboard.is_test_data
            else "Observed X data — coverage limited to disclosed sources"
        )
        for row in rows:
            row.setdefault("data_label", data_label)
            row.setdefault("test_data", leaderboard.is_test_data)
            row.setdefault("view", leaderboard.view)
            row.setdefault("metric", leaderboard.metric)
            row.setdefault("score_label", leaderboard.score_label)
            row.setdefault("denominator", leaderboard.denominator)
            row.setdefault("denominator_kind", leaderboard.denominator_kind)
            row.setdefault("generated_at", leaderboard.generated_at)
            row.setdefault("sources", leaderboard.sources)
            row.setdefault("limitations", leaderboard.limitations)
        return rows
    if kind == "events":
        return [event.to_dict() for event in events if not event.superseded_by]
    if kind == "pending":
        return [
            event.to_dict()
            for event in events
            if not event.superseded_by
            and event.verification_status in {"pending_review", "disputed", "inconclusive"}
        ]
    if kind == "rejected":
        return [
            event.to_dict()
            for event in events
            if not event.superseded_by
            and (
                event.verification_status == "rejected" or event.eligibility_status == "ineligible"
            )
        ]
    if kind == "poll_aggregates":
        return [poll.to_dict() for poll in polls]
    if kind == "coverage":
        return [cov.to_dict() for cov in coverage.values()]
    if kind == "config":
        return [competition.to_dict()]
    if kind == "snapshots":
        return [snapshot.to_dict() for snapshot in snapshots]
    if kind == "audit":
        return [record.to_dict() for record in audit]
    if kind == "agents":
        return list(agent_tasks)
    if kind == "summary":
        return [
            {
                "competition": competition.name,
                "generated_at": leaderboard.generated_at,
                "ruleset_version": competition.ruleset_version,
                "coverage_status": leaderboard.coverage_status,
                "finalization_status": competition.status,
                "total_confirmed": leaderboard.total_confirmed,
                "denominator": leaderboard.denominator,
                "project_count": len(competition.projects),
                "event_count": len([event for event in events if not event.superseded_by]),
                "poll_aggregate_count": len(polls),
                "limitations": leaderboard.limitations,
                "test_data": leaderboard.is_test_data,
            }
        ]
    raise ExportError(f"unsupported export kind: {kind}")


def build_export(
    kind: str,
    fmt: str,
    *,
    competition: Competition,
    leaderboard: Leaderboard,
    events: list[NormalizedEvent],
    polls: list[PollAggregate],
    coverage: dict[str, SourceCoverage],
    snapshots: list[Snapshot],
    audit: list[AuditRecord],
    agent_tasks: list[dict[str, Any]],
    fields: list[str] | None = None,
    confirm_sensitive: bool = False,
) -> ExportPackage:
    fmt = fmt.lower().strip()
    if fmt not in {"csv", "json", "html"}:
        raise ExportError("export format must be csv, json or html")
    rows = _rows_for_kind(
        kind,
        competition=competition,
        leaderboard=leaderboard,
        events=events,
        polls=polls,
        coverage=coverage,
        snapshots=snapshots,
        audit=audit,
        agent_tasks=agent_tasks,
    )
    if kind == "method" and rows:
        default_fields = ["rank", "name", "project_id", "method", "confirmed_votes", "measure"]
    elif kind == "coverage":
        default_fields = list(COVERAGE_FIELDS)
    elif kind == "snapshots":
        default_fields = list(SNAPSHOT_FIELDS)
    elif kind == "events" or kind in {"pending", "rejected"}:
        default_fields = list(EVENT_DEFAULT_FIELDS)
    elif kind == "audit":
        default_fields = [
            "audit_id",
            "timestamp",
            "actor",
            "action",
            "competition_id",
            "event_id",
            "note",
        ]
    elif kind == "agents":
        default_fields = [
            "task_id",
            "parent_task_id",
            "agent_role",
            "status",
            "requested_operation",
            "confidence",
            "error_code",
            "limitations",
        ]
    elif kind == "poll_aggregates":
        default_fields = [
            "aggregate_id",
            "project_id",
            "source_id",
            "option_label",
            "votes",
            "total_votes",
            "finished",
            "collected_at",
            "source_tool",
            "verification_status",
            "decision_reason",
        ]
    elif kind == "config":
        default_fields = list(rows[0].keys()) if rows else []
    elif kind == "summary":
        default_fields = list(rows[0].keys()) if rows else []
    else:
        default_fields = list(LEADERBOARD_DEFAULT_FIELDS)
    selected = list(dict.fromkeys(fields or default_fields))
    if not selected:
        raise ExportError("at least one export field is required")
    sensitive = [
        field_name
        for field_name in selected
        if field_name in SENSITIVE_ACCOUNT_FIELDS or field_name in SENSITIVE_EVIDENCE_FIELDS
    ]
    if sensitive and not confirm_sensitive:
        raise ExportError(
            "account-level or raw evidence fields require explicit confirmation: "
            + ", ".join(sorted(set(sensitive)))
        )
    usable = [
        {field_name: _field_value(row, field_name) for field_name in selected} for row in rows
    ]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"x-vote-{kind}-{stamp}-{stable_hash(competition.competition_id, stamp, length=6)}"

    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=selected, extrasaction="ignore")
        writer.writeheader()
        for row in usable:
            writer.writerow({key: _csv_safe(_cell(value)) for key, value in row.items()})
        content = buffer.getvalue().encode("utf-8-sig")
        return ExportPackage(
            filename=base + ".csv",
            media_type="text/csv; charset=utf-8",
            content=content,
            kind=kind,
            format=fmt,
            fields=selected,
            sensitive_included=sorted(set(sensitive)),
        )
    if fmt == "json":
        payload = {
            "exported_at": stamp,
            "kind": kind,
            "format": "x-vote/export/1",
            "test_data": leaderboard.is_test_data,
            "notice": (
                "SYNTHETIC TEST DATA — NOT LIVE X RESULTS"
                if leaderboard.is_test_data
                else "Observed data only; coverage reflects the disclosed sources."
            ),
            "competition": {
                "competition_id": competition.competition_id,
                "name": competition.name,
                "ruleset_version": competition.ruleset_version,
                "finalization_status": competition.status,
            },
            "coverage_status": leaderboard.coverage_status,
            "metric": leaderboard.metric,
            "score_label": leaderboard.score_label,
            "denominator": leaderboard.denominator,
            "denominator_kind": leaderboard.denominator_kind,
            "finalization_status": leaderboard.finalization_status,
            "generated_at": leaderboard.generated_at,
            "sources": leaderboard.sources,
            "limitations": leaderboard.limitations,
            "fields": selected,
            "records": usable,
        }
        return ExportPackage(
            filename=base + ".json",
            media_type="application/json; charset=utf-8",
            content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            kind=kind,
            format=fmt,
            fields=selected,
            sensitive_included=sorted(set(sensitive)),
        )
    table_rows = []
    for row in usable:
        cells = "".join(f"<td>{html.escape(str(_cell(value)))}</td>" for value in row.values())
        table_rows.append(f"<tr>{cells}</tr>")
    banner = (
        '<p class="banner">SYNTHETIC TEST DATA — NOT LIVE X RESULTS</p>'
        if leaderboard.is_test_data
        else ""
    )
    denominator_text = html.escape(
        str(leaderboard.denominator) if leaderboard.denominator is not None else "n/a"
    )
    source_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(source.get('label') or source.get('source_id')))}</td>"
        f"<td>{html.escape(str(source.get('source_type')))}</td>"
        f"<td>{html.escape(', '.join(source.get('methods') or []))}</td>"
        f"<td>{html.escape(str(source.get('collection_status')))}</td>"
        f"<td>{html.escape(str(source.get('last_checked')))}</td>"
        "</tr>"
        for source in leaderboard.sources
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>x-vote export — {html.escape(competition.name)}</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;margin:2rem;color:#17201d;background:#f7f7f3}}
table{{border-collapse:collapse;width:100%;font-size:.875rem;background:#fff}}
th,td{{border:1px solid #d8dcd6;padding:.5rem;text-align:left;vertical-align:top}}
th{{background:#edf1ec;position:sticky;top:0}}
.banner{{background:#fff3cd;padding:.75rem;font-weight:700}}
.limitations{{max-width:75ch}} code{{font-family:ui-monospace,monospace}}
</style></head><body>
<h1>{html.escape(competition.name)}</h1>
{banner}
<p>Kind: <code>{html.escape(kind)}</code> ·
Metric: <code>{html.escape(leaderboard.metric or "n/a")}</code> ·
Denominator: <code>{denominator_text}</code> ·
Coverage: <code>{html.escape(leaderboard.coverage_status)}</code> ·
Finalization: <code>{html.escape(leaderboard.finalization_status)}</code> ·
Ruleset: <code>{html.escape(competition.ruleset_version)}</code> ·
Generated: <code>{html.escape(leaderboard.generated_at)}</code></p>
<h2>Sources</h2>
<table><thead><tr>
<th>source</th><th>type</th><th>methods</th><th>collection status</th><th>last checked</th>
</tr></thead>
<tbody>{source_rows}</tbody></table>
<table><thead><tr>
{"".join(f"<th>{html.escape(field_name)}</th>" for field_name in selected)}
</tr></thead>
<tbody>{"".join(table_rows)}</tbody></table>
<h2>Limitations</h2>
<div class="limitations"><ul>
{"".join(f"<li>{html.escape(item)}</li>" for item in leaderboard.limitations)}
</ul></div>
</body></html>"""
    return ExportPackage(
        filename=base + ".html",
        media_type="text/html; charset=utf-8",
        content=document.encode("utf-8"),
        kind=kind,
        format=fmt,
        fields=selected,
        sensitive_included=sorted(set(sensitive)),
    )


def write_export(directory: Path, package: ExportPackage) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / package.filename
    path.write_bytes(package.content)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def available_fields(kind: str) -> list[str]:
    if kind in {"leaderboard", "provisional", "raw", "account", "weighted"}:
        return list(LEADERBOARD_DEFAULT_FIELDS) + [
            "project_id",
            "breakdown",
            "aggregate_breakdown",
            "confidence_breakdown",
            "score",
            "limitations",
        ]
    if kind in {"events", "pending", "rejected"}:
        return list(EVENT_DEFAULT_FIELDS) + [
            "account_id",
            "normalized_value",
            "raw_evidence_reference",
            "manual_decision",
            "limitations",
            "event_id",
        ]
    return []


def collection_status_label(status: str) -> str:
    try:
        return CollectionStatus(status).value.replace("_", " ")
    except ValueError:
        return status
