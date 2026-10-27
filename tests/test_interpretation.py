from __future__ import annotations

import pytest

from xvote.interpretation import interpret_instruction
from xvote.models import parse_timestamp


def test_instruction_extracts_projects_methods_timezone_and_weekday_deadline() -> None:
    draft = interpret_instruction(
        "Count votes for Alpha and Beta from this X post https://x.com/a/status/123, "
        "include replies and likes, ends Friday at 8 PM West Africa Time.",
        competition_name="Test",
    )
    assert draft["requires_confirmation"] is True
    competition = draft["interpretation"]["competition"]
    assert draft["interpretation"]["detected_projects"] == ["Alpha", "Beta"]
    assert draft["interpretation"]["detected_methods"] == ["reply", "like"]
    assert competition["timezone_name"] == "Africa/Lagos"
    deadline = parse_timestamp(competition["ends_at"])
    assert deadline is not None
    assert deadline.weekday() == 4
    assert deadline.hour == 19  # 20:00 WAT == 19:00 UTC
    assert any("Ranking method was not explicit" in item for item in draft["ambiguities"])


def test_instruction_detects_explicit_combined_ranking() -> None:
    draft = interpret_instruction(
        "Count votes for Alpha and Beta from https://x.com/a/status/123 using the poll and "
        "replies, then show the combined total.",
        competition_name="Test",
    )
    competition = draft["interpretation"]["competition"]
    assert competition["ranking"]["combine_confirmed_with_aggregates"] is True
    assert any("combined confirmed sum" in item for item in draft["ambiguities"])


def test_instruction_detects_unique_account_mode() -> None:
    draft = interpret_instruction(
        "Count unique accounts voting for Alpha from https://x.com/a/status/123 until 2030-01-01.",
        competition_name="Test",
    )
    assert draft["interpretation"]["competition"]["ranking"]["mode"] == "unique_account"


def test_instruction_requires_confirmation_and_rejects_empty() -> None:
    draft = interpret_instruction(
        "Count votes for Alpha from https://x.com/a/status/123 until 2030-01-01 12:00 UTC",
        competition_name="Test",
    )
    assert draft["activation_blocked"] is True
    assert draft["requires_confirmation"] is True
    assert "Nothing is collected" in draft["framing"]
    with pytest.raises(ValueError):
        interpret_instruction("   ")
