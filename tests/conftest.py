from __future__ import annotations

from pathlib import Path

import pytest

from xvote.service import AppService


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XVOTE_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("XVOTE_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.delenv("XVOTE_AUTHORIZED_USE_ACK", raising=False)
    monkeypatch.delenv("XVOTE_AI_ENDPOINT", raising=False)
    monkeypatch.delenv("XVOTE_AI_KEY", raising=False)
    monkeypatch.delenv("XVOTE_AI_CONSENT", raising=False)
    app = AppService()
    yield app
    app.cleanup_all()
