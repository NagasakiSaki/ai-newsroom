import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.collect import canonical, collect, demo_items, public_url
from newsroom.config import Settings
from newsroom.db import now
from newsroom.engine import Engine
from newsroom.llm import ModelClient
from newsroom.models import content_hash
from newsroom.render import export_document, validate_article, validate_evidence


@pytest.fixture
def engine(tmp_path):
    return Engine(Settings(data_dir=tmp_path, admin_token="test-only-token"))


def generate(engine):
    engine.enqueue("pipeline")
    assert engine.work_once()
    drafts = engine.store.all("drafts")
    assert len(drafts) == 1
    return drafts[0]


def approve(engine, draft):
    return engine.approve(draft["id"], draft["revision"], "测试编辑", True)




def test_collection_and_generation_are_idempotent(engine):
    generate(engine)
    engine.enqueue("pipeline")
    engine.work_once()
    assert len(engine.store.all("stories")) == 1
    assert len(engine.store.all("drafts")) == 1






def test_major_review_issue_blocks_approval_even_with_high_score(engine, monkeypatch):
    original = engine.model.complete
    def complete(stage, context, run, **kwargs):
        if stage == "review":
            return {"passed": True, "score": 99, "summary": "存在问题", "issues": [{"severity": "major", "category": "事实", "detail": "证据不足", "suggestion": "核实"}]}
        return original(stage, context, run, **kwargs)
    monkeypatch.setattr(engine.model, "complete", complete)
    draft = generate(engine)
    assert draft["status"] == "needs_revision"
    assert draft["revision"] == 2  # 自动修订仅一次，之后仍不得通过。
    with pytest.raises(ValueError):
        approve(engine, draft)


def test_extracted_quote_must_exist_and_citations_must_be_valid(engine):
    draft = generate(engine)
    evidence = copy.deepcopy(draft["evidence"])
    evidence["facts"][0]["quote"] = "并不存在于原文的一个虚假引用片段"
    with pytest.raises(ValueError):
        validate_evidence(evidence, demo_items()[0])
    bad = {**draft["article"], "markdown": draft["article"]["markdown"] + "错误事实 [F999]"}
    with pytest.raises(ValueError):
        validate_article(bad, draft["evidence"])














def test_api_authentication_and_secret_redaction(tmp_path):
    settings = Settings(data_dir=tmp_path, admin_token="test-token", llm_api_key="secret-llm", wechat_app_secret="secret-wechat")
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/state").status_code == 401
        assert client.post("/api/run").status_code == 401
        result = client.get("/api/state", headers={"Authorization": "Bearer test-token"})
        assert result.status_code == 200
        assert "secret-llm" not in result.text and "secret-wechat" not in result.text
        assert client.get("/").status_code == 200


def test_retry_requests_obey_model_budget(engine, monkeypatch):
    engine.settings.demo = False
    engine.settings.llm_api_key = "test-key"
    engine.settings.llm_model = "test-model"
    engine.settings.llm_base_url = "https://model.example/v1"
    engine.settings.max_calls = 2
    calls = []
    def post(self, url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(429, request=httpx.Request("POST", url))
    monkeypatch.setattr("newsroom.llm.request_completion", post)
    monkeypatch.setattr("newsroom.llm.time.sleep", lambda seconds: None)
    run = engine.new_run("test")
    with pytest.raises(ValueError, match="预算"):
        engine.model.complete("score", {"news": {}}, run)
    assert len(calls) == 2
    assert engine.store.get("runs", run["id"])["calls"] == 2
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}


def test_model_validates_output_schema_and_reviewer_credentials(engine, monkeypatch):
    engine.settings.demo = False
    engine.settings.llm_api_key = "writer-key"
    engine.settings.llm_model = "writer"
    engine.settings.llm_base_url = "https://writer.example/v1"
    engine.settings.review_base_url = "https://review.example/v1"
    run = engine.new_run("test")
    with pytest.raises(ValueError, match="独立配置"):
        engine.model.complete("review", {}, run)
    engine.settings.review_api_key = "review-key"
    engine.settings.review_model = "reviewer"
    requests = []
    def post(self, url, **kwargs):
        requests.append((url, kwargs))
        result = {"passed": True, "score": 88, "issues": [], "summary": "通过"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}], "usage": {"total_tokens": 100}}, request=httpx.Request("POST", url))
    monkeypatch.setattr("newsroom.llm.request_completion", post)
    result = engine.model.complete("review", {}, run)
    assert result["score"] == 88
    assert requests[0][0] == "https://review.example/v1/chat/completions"
    assert requests[0][1]["headers"]["Authorization"] == "Bearer review-key"


def test_rss_time_window_missing_date_and_partial_failure(monkeypatch):
    fresh = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    old = (datetime.now(timezone.utc)-timedelta(days=30)).strftime('%a, %d %b %Y %H:%M:%S GMT')
    xml = f'''<rss version="2.0"><channel><title>AI</title>
      <item><title>Fresh</title><link>https://example.org/new?utm_source=feed</link><pubDate>{fresh}</pubDate><description>New</description></item>
      <item><title>Old</title><link>https://example.org/old</link><pubDate>{old}</pubDate></item>
      <item><title>No date</title><link>https://example.org/unknown</link></item></channel></rss>'''
    def fetch(url, **kwargs):
        if 'broken' in url:
            raise ValueError('source unavailable')
        return xml
    monkeypatch.setattr('newsroom.collect.fetch_text', fetch)
    events = []
    items = collect([{"name": "broken", "url": "https://broken.example"}, {"name": "good", "url": "https://example.org/feed"}], 72, lambda *args: events.append(args))
    assert len(items) == 1 and items[0]["url"] == "https://example.org/new"
    assert any(e[0] == "source_error" for e in events)


def test_public_url_rejects_private_addresses_and_render_escapes(engine):
    with pytest.raises(ValueError):
        public_url('http://127.0.0.1/admin')
    with pytest.raises(ValueError):
        canonical('file:///etc/passwd')
    draft = generate(engine)
    draft["article"]["title"] = '<script>alert(1)</script>'
    draft["evidence"]["facts"][0]["quote"] = '<img src=x onerror="alert(1)">'
    html = export_document(draft)
    assert '<script>' not in html and 'onerror=' not in html
    from bs4 import BeautifulSoup
    assert all(img['src'].startswith('/media/') for img in BeautifulSoup(html, 'html.parser').find_all('img'))




def test_second_process_for_same_data_is_rejected(engine, monkeypatch):
    monkeypatch.setattr(engine, "loop", lambda: None)
    engine.start()
    other = Engine(engine.settings)
    try:
        with pytest.raises(ValueError, match="已有"):
            other.start()
    finally:
        engine.close()






def test_pending_preview_does_not_claim_human_approval(engine):
    draft = generate(engine)
    assert '人工最终审定' not in export_document(draft)
    approved = approve(engine, draft)
    assert 'AI 辅助采编' not in export_document(approved)
