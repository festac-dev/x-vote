from __future__ import annotations

from tests.helpers import make_competition
from xvote.textmatch import (
    find_hashtags,
    find_mentions,
    match_projects,
    normalize_text,
    phrase_matches,
    suggest_intent,
)


def test_phrase_matches_is_casefolded_and_boundary_aware() -> None:
    assert phrase_matches("I vote for ALPHA today", "alpha")
    assert phrase_matches("vote for Beta!", "beta")
    assert not phrase_matches("Betamax is not a vote", "Beta")
    assert not phrase_matches("Alphabet soup", "Alpha")
    assert not phrase_matches("anything", "  ")


def test_normalize_text_uses_nfkc_and_casefold() -> None:
    assert normalize_text("ＡＬＰＨＡ") == "alpha"


def test_hashtags_mentions_and_project_matching() -> None:
    assert find_hashtags("I back #Alpha and #beta_2") == ["alpha", "beta_2"]
    assert find_mentions("ask @Alpha_Team and @beta") == ["alpha_team", "beta"]
    competition = make_competition(projects=("Alpha",))
    project = competition.projects[0]
    project.name = "Project One"
    project.hashtags = ["alpha"]
    project.usernames = ["alpha_team"]
    competition.refresh_ruleset()

    assert (
        match_projects("I vote for Project One", competition.projects)[0].project_name
        == "Project One"
    )
    assert match_projects("#ALPHA", competition.projects)[0].identifier_type == "hashtag"
    assert match_projects("@alpha_team", competition.projects)[0].identifier_type == "username"
    assert match_projects("nothing here", competition.projects) == []


def test_intent_detection_is_conservative() -> None:
    assert suggest_intent("I vote for Alpha").classification == "vote"
    assert suggest_intent("Do NOT vote for Alpha, it is a scam").classification == "criticism"
    assert suggest_intent("Who should win this?").classification == "question"
    assert suggest_intent("Alpha").classification == "ambiguous"
