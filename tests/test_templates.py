from __future__ import annotations

from xvote.models import SourceType, VotingMethod
from xvote.templates import list_templates


def test_templates_are_valid_and_unique() -> None:
    payload = list_templates()
    templates = payload["templates"]
    assert templates
    assert "defaults only" in payload["note"]
    ids = [template["id"] for template in templates]
    assert len(ids) == len(set(ids))
    source_types = {item.value for item in SourceType}
    methods = {item.value for item in VotingMethod}
    for template in templates:
        assert template["name"]
        assert template["description"]
        assert template["source_type"] in source_types
        assert template["methods"]
        assert set(template["methods"]) <= methods
        assert "token" not in str(template).casefold()
        assert "cookie" not in str(template).casefold()


def test_templates_keep_aggregates_separate() -> None:
    templates = {item["id"]: item for item in list_templates()["templates"]}
    for template_id in ("native_poll", "external_aggregate", "screenshot_ocr"):
        template = templates[template_id]
        assert template["ranking"]["mode"] == "separate"
        assert template["ranking"].get("combine_confirmed_with_aggregates") is not True
