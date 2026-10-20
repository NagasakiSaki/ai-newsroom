import copy
from datetime import datetime, timezone
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.db import now
from newsroom.engine import Engine
from newsroom.schedule import SchedulePlan, next_occurrence
from newsroom.wechat import WechatError


@pytest.fixture
def engine(tmp_path):
    return Engine(Settings(data_dir=tmp_path, admin_token="test"))


def draft(engine):
    engine.enqueue("pipeline")
    engine.work_once()
    return engine.store.all("drafts")[0]


def test_one_click_automatically_delivers_before_human_review(engine):
    d = draft(engine)
    assert d["status"] == "pending_human"
    assert not d["approved_hash"]
    assert d["delivery"]["state"] == "ready"
    package = engine.settings.data_dir / "exports" / d["id"] / "v1/handoff.zip"
    with ZipFile(package) as archive:
        assert {"article.html", "wechat.html", "article.md", "sources.json", "review.json", "README.txt"} <= set(archive.namelist())
        assert '人工最终审定' not in archive.read('article.html').decode()
    assert not any(j["kind"] in {"publish", "poll"} for j in engine.store.all("jobs"))
    assert engine.store.all("runs")[0]["status"] == "done"


def test_approval_never_calls_wechat_when_publish_is_disabled(engine, monkeypatch):
    # 发布默认关闭：审批只改变本地状态，不创建草稿、不提交发布，也不排队发布任务。
    d = draft(engine)
    monkeypatch.setattr(engine.wechat, "create_draft", lambda _: pytest.fail("本地模式不应触发微信"))
    monkeypatch.setattr(engine.wechat, "add_draft", lambda payload: pytest.fail("发布未启用时不应创建草稿"))
    result = engine.approve(d["id"], 1, "人工编辑", True)
    assert result["status"] == "ready_to_publish"
    assert result["approved_hash"] == engine.final_hash(result)
    assert not engine.work_once()
    assert not any(j["kind"] in {"publish", "publish_check"} for j in engine.store.all("jobs"))
    assert engine.store.all("publications") == []
    assert 'AI 辅助采编' not in (engine.settings.data_dir / 'exports' / d['id'] / 'v1/article.html').read_text(encoding='utf-8')
    with pytest.raises(ValueError, match="未启用"):
        engine.publish(d["id"], 1)
    with pytest.raises(ValueError, match="未启用"):
        engine.queue_publish(d["id"], 1)


def test_edit_rebuilds_current_handoff_only_after_review(engine):
    d = draft(engine)
    engine.approve(d["id"], 1, "编辑", True)
    article = {**d["article"], "title": "人工改过的演示标题"}
    changed = engine.edit(d["id"], 1, article)
    assert changed["revision"] == 2 and changed["delivery"] is None and changed["approved_hash"] is None
    assert not (engine.settings.data_dir / "exports" / d["id"] / "v2/handoff.zip").exists()
    engine.work_once()
    current = engine.store.get("drafts", d["id"])
    assert current["status"] == "pending_human" and current["delivery"]["revision"] == 2
    assert (engine.settings.data_dir / "exports" / d["id"] / "v2/handoff.zip").is_file()
    with pytest.raises(ValueError):
        engine.approve(d["id"], 1, "编辑", True)


def test_rejected_draft_cannot_be_downloaded_as_handoff(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token="test"), start_worker=False)
    d = draft(app.state.engine)
    app.state.engine.reject(d["id"], 1, "证据不足")
    with TestClient(app) as client:
        result = client.get(f"/api/drafts/{d['id']}/export/zip", headers={"Authorization": "Bearer test"})
        assert result.status_code == 409


def test_daily_times_use_beijing_and_cross_midnight():
    plan = SchedulePlan().model_dump()
    # UTC 00:59 = 北京 08:59。
    assert next_occurrence(plan, datetime(2026, 10, 19, 0, 59, tzinfo=timezone.utc)) == "2026-10-19T01:00:00+00:00"
    assert next_occurrence(plan, datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)) == "2026-10-19T10:00:00+00:00"
    assert next_occurrence(plan, datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)) == "2026-10-20T01:00:00+00:00"


def test_persisted_schedule_survives_new_engine_and_deduplicates_slot(engine):
    plan = engine.get_schedule()
    plan["next_run_at"] = "2026-10-19T01:00:00+00:00"
    engine.store.put("schedule", plan)
    current = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)
    first = engine.schedule_tick(current)
    assert first["trigger"] == "schedule"
    assert engine.schedule_tick(current) is None
    restored = Engine(engine.settings)
    assert restored.get_schedule()["next_run_at"] == "2026-10-19T10:00:00+00:00"
    # 模拟在持久化任务后、推进计划前崩溃：已有时段任务不被重放。
    restored.store.put("schedule", plan)
    assert restored.schedule_tick(current)["id"] == first["id"]
    assert len(engine.store.all("jobs")) == 1


def test_offline_missed_slots_coalesce_and_pausing_stops_schedule(engine):
    plan = engine.get_schedule()
    plan["next_run_at"] = "2026-10-17T01:00:00+00:00"
    engine.store.put("schedule", plan)
    engine.schedule_tick(datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    assert len(engine.store.all("jobs")) == 1
    assert engine.get_schedule()["next_run_at"] == "2026-10-20T01:00:00+00:00"
    engine.set_schedule({"enabled": False, "mode": "daily", "times": ["09:00", "18:00"], "interval_minutes": 30})
    assert engine.schedule_tick(datetime(2099, 1, 1, tzinfo=timezone.utc)) is None


def test_schedule_validation_and_interval():
    with pytest.raises(ValueError):
        SchedulePlan(times=["24:00"])
    with pytest.raises(ValueError):
        SchedulePlan(interval_minutes=0)
    plan = SchedulePlan(mode="interval", interval_minutes=30).model_dump()
    assert next_occurrence(plan, datetime(2026, 10, 19, 1, tzinfo=timezone.utc)) == "2026-10-19T01:30:00+00:00"


def test_formal_publication_routes_are_absent_and_schedule_is_authenticated(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token="test"), start_worker=False)
    headers = {"Authorization": "Bearer test"}
    with TestClient(app) as client:
        assert client.put("/api/schedule", json=SchedulePlan().model_dump()).status_code == 401
        assert client.put("/api/schedule", headers=headers, json={"enabled": True, "mode": "daily", "times": ["09:00", "18:00"], "interval_minutes": 30}).status_code == 200
        assert client.post("/api/drafts/unused/queue", headers=headers, json={"revision": 1}).status_code == 404
        assert client.post("/api/drafts/unused/reconcile", headers=headers, json={"publish_id": "1"}).status_code == 404


def test_manual_publication_record_requires_review_and_wechat_url(engine, monkeypatch):
    monkeypatch.setattr(engine, "verify_source", lambda d, **kw: d)
    d = draft(engine)
    d["demo"] = False
    engine.store.put("drafts", d)
    with pytest.raises(ValueError):
        engine.record_publication(d["id"], 1, "https://mp.weixin.qq.com/s/abc", "编辑")
    engine.approve(d["id"], 1, "编辑", True)
    with pytest.raises(ValueError):
        engine.record_publication(d["id"], 1, "javascript:alert(1)", "编辑")
    result = engine.record_publication(d["id"], 1, "https://mp.weixin.qq.com/s/abc", "编辑")
    assert result["status"] == "manual_published"
    with pytest.raises(ValueError):
        engine.edit(d["id"], 1, d["article"])


def test_optional_wechat_draft_sync_does_not_retry_unknown_creation(engine, monkeypatch):
    monkeypatch.setattr(engine, "verify_source", lambda d, **kw: d)
    d = draft(engine)
    d["demo"] = False
    engine.store.put("drafts", d)
    engine.settings.demo = False
    engine.settings.delivery_mode = "wechat_draft"
    calls = []
    def create(_):
        calls.append(True)
        raise WechatError("草稿创建结果未确认")
    monkeypatch.setattr(engine.wechat, "create_draft", create)
    result = engine.deliver(d["id"], 1)
    assert result["delivery"]["state"] == "ready"
    assert result["delivery"]["wechat_state"] == "needs_check"
    engine.approve(d["id"], 1, "编辑", True)
    engine.deliver(d["id"], 1)
    assert len(calls) == 1


def test_draft_sync_never_touches_live_wechat_in_demo(engine, monkeypatch):
    d = draft(engine)
    engine.settings.delivery_mode = "wechat_draft"
    monkeypatch.setattr(engine.wechat, "create_draft", lambda _: pytest.fail("演示稿不能发送到微信"))
    engine.deliver(d["id"], 1)
    assert engine.store.get("drafts", d["id"])["delivery"]["wechat_state"] == "needs_check"


def test_restart_reconciles_inflight_publication_without_replay(engine, monkeypatch):
    # 中断后旧发布任务标记 interrupted，发布记录转为结果待核对，只查询对账，不重新提交。
    d = draft(engine)
    d.update(status="queued", delivery=None)
    engine.store.put("drafts", d)
    engine.store.put("publications", {"id": "pub-1", "draft_id": d["id"], "revision": 1, "approved_hash": "x",
                                      "account": "wx", "status": "submitting", "intent": "submit",
                                      "media_id": "MEDIA-1", "created_at": now(), "updated_at": now()})
    job = engine.enqueue("publish", "pub-1")
    checked = []
    monkeypatch.setattr(engine, "loop", lambda: None)
    monkeypatch.setattr(engine, "check_publication", lambda publication_id: checked.append(publication_id))
    engine.start()
    try:
        assert engine.store.get("drafts", d["id"])["status"] == "pending_human"
        assert engine.store.get("jobs", job["id"])["status"] == "interrupted"
        restored = engine.store.get("publications", "pub-1")
        assert restored["status"] == "unknown" and restored["attention"] == "interrupted"
        assert any(row["kind"] == "publish_check" and row["subject"] == "pub-1" for row in engine.store.all("jobs"))
        engine.work_once()
        engine.work_once()
        assert checked == ["pub-1"]
        assert engine.store.get("drafts", d["id"])["delivery"]["state"] == "ready"
    finally:
        engine.close()


def test_evidence_change_breaks_manual_record_hash(engine):
    d = draft(engine)
    original = engine.final_hash(d)
    d["evidence"]["facts"][0]["text"] += "changed"
    assert engine.final_hash(d) != original


def test_all_feed_errors_are_reported_as_failure_not_success(engine, monkeypatch):
    engine.settings.demo = False
    def empty(sources, age, log, **kwargs):
        log("source_error", "all sources unavailable")
        return []
    monkeypatch.setattr("newsroom.collect.collect", empty)
    engine.enqueue("pipeline")
    engine.work_once()
    assert engine.store.all("runs")[0]["status"] == "failed"


def test_two_runs_share_daily_quota_without_first_run_consuming_it(engine, monkeypatch):
    from newsroom.collect import demo_items
    first = demo_items()[0]
    first.update(id="topic-1", title="Completely new compiler architecture")
    second = {**copy.deepcopy(first), "id": "topic-2", "title": "Robotics vision sensor study", "url": "https://example.com/robotics"}
    monkeypatch.setattr("newsroom.collect.demo_items", lambda: [first, second])
    engine.settings.max_drafts_day = 2
    engine.settings.max_drafts_run = 1
    engine.enqueue("pipeline")
    engine.work_once()
    assert len(engine.store.all("drafts")) == 1
    engine.enqueue("pipeline")
    engine.work_once()
    assert len(engine.store.all("drafts")) == 2
    assert all(d["delivery"]["state"] == "ready" for d in engine.store.all("drafts"))


def test_blocked_ai_review_never_creates_handoff(engine, monkeypatch):
    original = engine.model.complete
    def complete(stage, context, run, **kwargs):
        if stage == "review":
            return {"passed": False, "score": 20, "summary": "证据不足", "issues": [{"severity": "blocker", "category": "fact", "detail": "unsupported", "suggestion": "verify"}]}
        return original(stage, context, run, **kwargs)
    monkeypatch.setattr(engine.model, "complete", complete)
    d = draft(engine)
    assert d["status"] == "needs_revision"
    assert not (engine.settings.data_dir / "exports").exists()
