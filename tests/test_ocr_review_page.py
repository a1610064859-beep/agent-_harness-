"""The human-review page is served separately from invoice upload."""
import dataclasses
from pathlib import Path

from fiscal_harness import db, importer, web
from fiscal_harness.llm import ScriptedLLM


def test_review_page_route_and_client_only_policy(settings, tmp_path, monkeypatch):
    settings = dataclasses.replace(settings, db_path=tmp_path / "review.db")
    importer.reset_and_import(db.connect(settings.db_path), settings.sample_xlsx, settings.rules_docx)
    monkeypatch.setattr(web, "from_settings", lambda _: ScriptedLLM([]))
    app = web.create_app(settings)
    route = next(r for r in app.routes if getattr(r, "path", None) == "/ocr-review")
    response = route.endpoint()
    assert response.headers["cache-control"] == "no-store"
    html = Path(response.path).read_text(encoding="utf-8")
    assert "connect-src 'none'" in html
    assert "crypto.subtle.digest('SHA-256'" in html
