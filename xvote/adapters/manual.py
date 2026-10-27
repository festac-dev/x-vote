"""Manual CSV/JSON import and OCR evidence parsing.

Imported content is untrusted data. It is validated field by field, never
executed, and every record retains its provenance and confidence label.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import CONFIDENCE_ORDER, Confidence, VotingMethod, stable_hash


class ImportValidationError(ValueError):
    pass


@dataclass
class ManualRecord:
    record_id: str
    project: str
    votes: int | None
    method: str
    account_id: str = ""
    source_url: str = ""
    event_timestamp: str = ""
    evidence_reference: str = ""
    confidence: str = Confidence.MODERATE.value
    note: str = ""
    content_status: str = "unchanged"
    row_number: int = 0
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


_ALLOWED_CONFIDENCE = {item.value for item in Confidence}
_MAX_ROWS = 50_000
_MAX_TEXT = 2_000


def _validate_confidence(value: str) -> str:
    label = (value or Confidence.MODERATE.value).strip().lower().replace(" ", "_")
    if label not in _ALLOWED_CONFIDENCE:
        raise ImportValidationError(f"unsupported confidence label: {value!r}")
    return label


def _safe_text(value: Any, limit: int = _MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _parse_votes(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"\d{1,9}", text):
        raise ImportValidationError(f"votes must be a non-negative integer, got {value!r}")
    return int(text)


_CONTENT_STATUS_ALIASES = {
    "": "unchanged",
    "unchanged": "unchanged",
    "ok": "unchanged",
    "active": "unchanged",
    "edited": "edited",
    "edit": "edited",
    "modified": "edited",
    "deleted": "deleted",
    "delete": "deleted",
    "removed": "deleted",
}


def _parse_content_status(row: dict[str, Any]) -> str:
    raw = _safe_text(row.get("content_status") or row.get("source_content_status"), 40).casefold()
    if not raw:
        if str(row.get("deleted", "")).strip().casefold() in {"1", "true", "yes"}:
            raw = "deleted"
        elif str(row.get("edited", "")).strip().casefold() in {"1", "true", "yes"}:
            raw = "edited"
    status = _CONTENT_STATUS_ALIASES.get(raw)
    if status is None:
        raise ImportValidationError(f"unsupported content_status: {raw!r}")
    return status


def _resolve_method(value: str, *, aggregate: bool) -> str:
    raw = (value or "").strip()
    if not raw:
        return VotingMethod.MANUAL_TOTAL.value if aggregate else VotingMethod.REPLY.value
    aliases = {
        "manual": VotingMethod.MANUAL_TOTAL.value,
        "total": VotingMethod.MANUAL_TOTAL.value,
        "manual_total": VotingMethod.MANUAL_TOTAL.value,
        "csv": VotingMethod.MANUAL_TOTAL.value,
        "json": VotingMethod.MANUAL_TOTAL.value,
        "external": VotingMethod.EXTERNAL_AGGREGATE.value,
    }
    method = aliases.get(raw.lower(), raw)
    valid = {item.value for item in VotingMethod}
    if method not in valid:
        raise ImportValidationError(f"unsupported voting method: {value!r}")
    return method


class ManualImportAdapter:
    name = "manual-import"
    auth_tier = "none"

    def capabilities(self) -> dict[str, bool]:
        return {
            "poll": False,
            "replies": False,
            "individual_likers": False,
            "individual_reposters": False,
            "auth_required": False,
        }

    def parse_csv(self, payload: str, *, source_label: str = "") -> list[ManualRecord]:
        text = payload.lstrip("\ufeff")
        if len(text.encode("utf-8", errors="replace")) > 10 * 1024 * 1024:
            raise ImportValidationError("CSV import exceeds the 10 MB limit")
        if not text.strip():
            raise ImportValidationError("CSV import is empty")
        try:
            dialect = csv.Sniffer().sniff(text[:4096])
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            raise ImportValidationError("CSV import has no header row")
        required = {"project"}
        if not required.issubset({(name or "").strip().lower() for name in reader.fieldnames}):
            raise ImportValidationError("CSV import requires a 'project' column")
        records: list[ManualRecord] = []
        for index, row in enumerate(reader, start=2):
            if index > _MAX_ROWS + 1:
                raise ImportValidationError("CSV import exceeds the row limit")
            normalized = {
                (key or "").strip().lower(): value for key, value in row.items() if key is not None
            }
            if not any(str(value or "").strip() for value in normalized.values()):
                continue
            record = self._record_from_row(normalized, index=index, source_label=source_label)
            records.append(record)
        if not records:
            raise ImportValidationError("CSV import contained no records")
        return records

    def parse_json(self, payload: str, *, source_label: str = "") -> list[ManualRecord]:
        if len(payload.encode("utf-8", errors="replace")) > 10 * 1024 * 1024:
            raise ImportValidationError("JSON import exceeds the 10 MB limit")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ImportValidationError(f"invalid JSON: line {exc.lineno}") from exc
        if isinstance(data, dict):
            rows = data.get("records")
            if rows is None:
                rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            raise ImportValidationError("JSON import must be an object or array")
        if not isinstance(rows, list) or not rows:
            raise ImportValidationError("JSON import contains no records")
        if len(rows) > _MAX_ROWS:
            raise ImportValidationError("JSON import exceeds the row limit")
        records = []
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise ImportValidationError(f"JSON record {index} is not an object")
            normalized = {str(key).strip().lower(): value for key, value in row.items()}
            records.append(
                self._record_from_row(normalized, index=index, source_label=source_label)
            )
        return records

    def _record_from_row(
        self, row: dict[str, Any], *, index: int, source_label: str
    ) -> ManualRecord:
        project = _safe_text(row.get("project") or row.get("project_name") or row.get("name"))
        if not project:
            raise ImportValidationError(f"row {index}: project is required")
        votes = _parse_votes(row.get("votes") or row.get("total") or row.get("count"))
        account_id = _safe_text(row.get("account_id") or row.get("user_id") or row.get("username"))
        aggregate = votes is not None and not account_id
        method = _resolve_method(_safe_text(row.get("method")), aggregate=aggregate)
        if aggregate and method not in {
            VotingMethod.MANUAL_TOTAL.value,
            VotingMethod.EXTERNAL_AGGREGATE.value,
            VotingMethod.OCR_TOTAL.value,
        }:
            method = VotingMethod.MANUAL_TOTAL.value
        record_id = _safe_text(row.get("record_id") or row.get("id"), 200)
        if not record_id:
            record_id = "rec_" + stable_hash(
                source_label, project, votes, account_id, index, length=16
            )
        confidence = _validate_confidence(_safe_text(row.get("confidence"), 100))
        source_url = _safe_text(row.get("source_url") or row.get("url"), 500)
        evidence = _safe_text(row.get("evidence_reference") or row.get("evidence"), 500)
        limitations: list[str] = []
        if aggregate:
            limitations.append(
                "Manual aggregate total: individual voter verification is unavailable."
            )
        if not source_url:
            limitations.append("No source URL was supplied for this manual record.")
        if not evidence:
            evidence = f"manual:{source_label or 'import'}:{record_id}"
        content_status = _parse_content_status(row)
        if content_status == "edited":
            limitations.append("Operator reported that the source content was edited.")
        elif content_status == "deleted":
            limitations.append("Operator reported that the source content was deleted.")
        return ManualRecord(
            record_id=record_id,
            project=project,
            votes=votes,
            method=method,
            account_id=account_id,
            source_url=source_url,
            event_timestamp=_safe_text(row.get("event_timestamp") or row.get("timestamp"), 100),
            evidence_reference=evidence,
            confidence=confidence,
            note=_safe_text(row.get("note") or row.get("notes"), 500),
            content_status=content_status,
            row_number=index,
            limitations=limitations,
        )

    def parse(
        self, payload: str, *, format_hint: str = "", source_label: str = ""
    ) -> list[ManualRecord]:
        hint = format_hint.lower().strip()
        if hint == "json":
            return self.parse_json(payload, source_label=source_label)
        if hint == "csv":
            return self.parse_csv(payload, source_label=source_label)
        stripped = payload.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return self.parse_json(payload, source_label=source_label)
        return self.parse_csv(payload, source_label=source_label)


class OCRUnavailable(RuntimeError):
    pass


class OCREvidenceAdapter:
    name = "ocr-evidence"
    auth_tier = "none"
    MAX_IMAGE_BYTES = 8 * 1024 * 1024
    MAGIC = {
        b"\x89PNG\r\n\x1a\n": "png",
        b"\xff\xd8\xff": "jpeg",
        b"RIFF": "webp",
    }

    def capabilities(self) -> dict[str, bool]:
        return {
            "poll": False,
            "replies": False,
            "individual_likers": False,
            "individual_reposters": False,
            "auth_required": False,
        }

    def validate_image(self, data: bytes) -> str:
        if not data:
            raise ImportValidationError("empty image upload")
        if len(data) > self.MAX_IMAGE_BYTES:
            raise ImportValidationError("image exceeds the 8 MB limit")
        for magic, kind in self.MAGIC.items():
            if data.startswith(magic):
                if kind == "webp" and data[8:12] != b"WEBP":
                    continue
                return kind
        raise ImportValidationError("only PNG, JPEG or WebP images are supported")

    def extract_text(self, data: bytes) -> str:
        self.validate_image(data)
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as exc:
            raise OCRUnavailable(
                "OCR engine is not installed (Pillow + pytesseract and the "
                "tesseract binary are required)"
            ) from exc
        try:
            image = Image.open(io.BytesIO(data))
            image.verify()
            image = Image.open(io.BytesIO(data))
            return pytesseract.image_to_string(image)[:20_000]
        except Exception as exc:
            raise OCRUnavailable(f"OCR failed: {type(exc).__name__}") from exc

    def parse_text(
        self,
        text: str,
        *,
        confidence: str = Confidence.LOW.value,
        evidence_reference: str = "",
        source_label: str = "ocr",
    ) -> list[ManualRecord]:
        """Parse operator-reviewed OCR text. Always requires human review."""
        label = _validate_confidence(confidence)
        records: list[ManualRecord] = []
        pattern = re.compile(
            r"^\s*(?P<project>[^\d:]{1,120}?)\s*(?:[:=\-–]|\s{2,})\s*(?P<votes>\d{1,9})\s*(?:votes?)?\s*$",
            re.IGNORECASE,
        )
        for line in text.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            project = match.group("project").strip(" -–=:")
            if not project:
                continue
            record_id = "ocr_" + stable_hash(
                source_label, project, match.group("votes"), line, length=16
            )
            records.append(
                ManualRecord(
                    record_id=record_id,
                    project=project,
                    votes=int(match.group("votes")),
                    method=VotingMethod.OCR_TOTAL.value,
                    evidence_reference=evidence_reference or f"ocr:{source_label}:{record_id}",
                    confidence=label,
                    note="OCR-derived aggregate; requires operator review.",
                    limitations=[
                        "OCR text may misread digits or labels; treated as manually "
                        "supplied evidence.",
                        "Individual voter verification is unavailable for OCR aggregates.",
                    ],
                )
            )
        if not records:
            raise ImportValidationError("OCR text contained no 'Project: N votes' lines")
        return records


def confidence_rank(label: str) -> int:
    try:
        return CONFIDENCE_ORDER[Confidence(label)]
    except ValueError:
        return 0
