from __future__ import annotations

import asyncio

import pytest

from xvote.agents.providers import ModelProvider
from xvote.agents.runtime import (
    AgentConfigurationError,
    AgentRuntime,
    InvalidAgentOutput,
    validate_result,
)
from xvote.config import AcquisitionConfig
from xvote.ranking import build_leaderboard

from .helpers import make_competition, make_event


def _plan_data():
    competition = make_competition()
    event = make_event(competition, "Alpha")
    event.normalized_value["text_excerpt"] = "Who should win this?"
    poll_event = make_event(
        competition,
        "Beta",
        method="native_poll",
        account="",
        confidence="not_individually_verifiable",
    )
    events = [event, poll_event]
    leaderboard = build_leaderboard(competition, events, [], {})
    return competition, events, leaderboard


def test_deterministic_agent_plan_runs_without_ai() -> None:
    competition, events, leaderboard = _plan_data()
    config = AcquisitionConfig()
    runtime = AgentRuntime(config)
    tasks = asyncio.run(runtime.run_plan(competition, events, [], {}, leaderboard))
    roles = {task.agent_role for task in tasks}
    assert "orchestrator" in roles
    assert "leaderboard_verifier" in roles
    assert "classification" in roles
    assert all(task.status == "succeeded" for task in tasks)
    classification = next(task for task in tasks if task.agent_role == "classification")
    assert classification.result["model_used"] is False
    assert classification.result["model_available"] is False


def test_agent_plan_skips_unchanged_work_on_second_run() -> None:
    competition, events, leaderboard = _plan_data()
    runtime = AgentRuntime(AcquisitionConfig())
    first = asyncio.run(runtime.run_plan(competition, events, [], {}, leaderboard))
    second = asyncio.run(runtime.run_plan(competition, events, [], {}, leaderboard))
    assert len(second) == len(first)
    assert all(task.status == "skipped" for task in second)
    assert all("Unchanged inputs" in " ".join(task.limitations) for task in second)


def test_agent_max_depth_is_enforced() -> None:
    competition, events, leaderboard = _plan_data()
    config = AcquisitionConfig()
    config.agents_max_depth = 1
    runtime = AgentRuntime(config)
    with pytest.raises(AgentConfigurationError):
        asyncio.run(runtime.run_plan(competition, events, [], {}, leaderboard))


def test_agent_result_schema_rejects_unknown_fields() -> None:
    with pytest.raises(InvalidAgentOutput):
        validate_result("classification", {"events_examined": 1, "override_counts": True})
    with pytest.raises(InvalidAgentOutput):
        validate_result("not-a-role", {})
    validated = validate_result("classification", {"events_examined": 1})
    assert validated == {"events_examined": 1}


class SuggestingProvider(ModelProvider):
    name = "test-suggesting"

    def available(self) -> tuple[bool, str]:
        return True, ""

    async def classify(self, text: str, projects: list[str]) -> dict[str, object]:
        return {
            "classification": "spam",
            "confidence": "high",
            "reason": "unit test suggestion; must never change counts",
        }


def test_ai_suggestions_never_change_event_status_or_counts() -> None:
    competition, events, leaderboard = _plan_data()
    before = [
        (event.event_id, event.project_id, event.eligibility_status, event.verification_status)
        for event in events
    ]
    runtime = AgentRuntime(AcquisitionConfig(), provider=SuggestingProvider())
    tasks = asyncio.run(runtime.run_plan(competition, events, [], {}, leaderboard))
    classification = next(task for task in tasks if task.agent_role == "classification")
    assert classification.result["model_used"] is True
    after = [
        (event.event_id, event.project_id, event.eligibility_status, event.verification_status)
        for event in events
    ]
    assert after == before
    assert (
        events[0].normalized_value["classification_suggestion"]["model_suggestion"][
            "classification"
        ]
        == "spam"
    )
