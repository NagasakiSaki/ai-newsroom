import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.db import now
from newsroom.engine import Engine
from newsroom.workflow_control import usage_summary
from test_publication import FakeWechat, enable, make_engine


def until(condition, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError('worker did not reach expected checkpoint')


@pytest.mark.parametrize('operation', ['resume', 'cancel'])
def test_pause_retains_paid_result_and_resume_never_replays_write(tmp_path, operation):
    engine = Engine(Settings(data_dir=tmp_path, admin_token='test', review_mode='off', fast_mode=True))
    started, release = threading.Event(), threading.Event()
    original = engine.model.complete
    writes = []

    def complete(stage, *args, **kwargs):
        result = original(stage, *args, **kwargs)
        if stage == 'write':
            writes.append(result)
            started.set()
            assert release.wait(5)
        return result

    engine.model.complete = complete
    job = engine.enqueue('pipeline')
    worker = threading.Thread(target=engine.work_once)
    worker.start()
    try:
        assert started.wait(5)
        engine.controls.request('pause', job['id'])
        # No HTTP force-close: the response is saved before the next checkpoint.
        assert engine.store.get('jobs', job['id'])['status'] == 'running'
        release.set()
        until(lambda: engine.store.get('jobs', job['id'])['status'] == 'paused')
        draft = engine.store.all('drafts')[0]
        assert draft['article'] == writes[0]
        assert draft['status'] == 'needs_ai_review'
        assert engine.enqueue('pipeline')['id'] == job['id']
        engine.controls.request(operation, job['id'])
        worker.join(8)
        assert not worker.is_alive()
        assert len(writes) == 1
        final = engine.store.get('jobs', job['id'])
        assert final['status'] == ('done' if operation == 'resume' else 'cancelled')
        if operation == 'cancel':
            assert engine.store.get('runs', final['run_id'])['generated'] == 1
            engine.enqueue('pipeline')
            assert engine.work_once() is False
            assert engine.controls.state()['dispatch_paused']
        else:
            assert engine.store.all('drafts')[0]['status'] == 'pending_human'
            assert not engine.controls.state()['dispatch_paused']
    finally:
        release.set()
        engine.stop_event.set()
        worker.join(8)


def test_cancel_queue_and_dispatch_gate_do_not_replay_tasks(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    job = engine.enqueue('pipeline')
    engine.controls.request('cancel', job['id'])
    assert engine.store.get('jobs', job['id'])['status'] == 'cancelled'
    engine.controls.request('pause')
    assert engine.schedule_tick(datetime.now(timezone.utc) + timedelta(days=1)) is None
    assert engine.collection_tick() is None
    assert not engine.work_once()
    engine.controls.request('resume')
    assert not engine.controls.state()['dispatch_paused']


def test_readonly_second_engine_cannot_clear_active_workers_pause_request(tmp_path):
    settings = Settings(data_dir=tmp_path)
    engine = Engine(settings)
    job = engine.enqueue('pipeline')
    engine.controls.begin(job)
    engine.controls.request('pause', job['id'])
    another = Engine(settings)
    assert another.controls.state()['requested'] == 'pause'
    assert engine.controls.state()['job_id'] == job['id']
    engine.controls.end()


def test_usage_tracks_unknown_and_warns_without_a_hard_limit(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    engine.store.put('preferences', {'id': 'usage_alert', 'run_alert_tokens': 100})
    run = engine.new_run('pipeline')
    engine.model.record(run, 'write', 'fake-real', {'total_tokens': 101, 'prompt_tokens': 40}, 'ok')
    engine.model.record(run, 'write', 'fake-real', {}, 'retryable_error')
    engine.model.record(run, 'extract', 'fake-real', {}, 'cache_hit')
    engine.model.record(run, 'write', 'fake-real', {'total_tokens': 25}, 'ok')
    stored = engine.store.get('runs', run['id'])
    assert stored['token_usage']['total_tokens'] == 126
    assert stored['unknown_usage_requests'] == 1
    assert stored['status'] == 'running'
    assert len([e for e in engine.store.events() if e['stage'] == 'token_alert']) == 1
    summary = usage_summary(engine.store)
    assert summary['reported_tokens'] == 126
    assert summary['requests'] == 3
    assert summary['unknown_usage_requests'] == 1


def test_control_and_one_click_routes_require_auth_and_confirmation(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test'), start_worker=False)
    with TestClient(app) as client:
        assert client.post('/api/workflow/control', json={'action': 'pause'}).status_code == 401
        client.headers['Authorization'] = 'Bearer test'
        assert client.post('/api/workflow/control', json={'action': 'pause'}).json()['dispatch_paused']
        assert client.put('/api/usage/preferences', json={'run_alert_tokens': -1}).status_code == 422
        assert client.put('/api/usage/preferences', json={'run_alert_tokens': 25000}).status_code == 200
        state = client.get('/api/state').json()
        assert state['usage']['preferences']['run_alert_tokens'] == 25000
        assert state['workflow_control']['dispatch_paused']
        assert client.post('/api/drafts/no-such-draft/wechat-draft', json={'revision': 1}).status_code == 409


def checked_real_draft(engine):
    # Real provenance fields with wholly local demo fixture content. No external requests.
    engine.enqueue('pipeline')
    engine.work_once()
    draft = engine.store.all('drafts')[0]
    story = engine.store.get('stories', draft['story_id'])
    story.update(source_id='techcrunch-ai', source='TechCrunch AI', demo=False,
                 url='https://techcrunch.com/2026/10/20/local-test/')
    for fact in draft['evidence']['facts']:
        fact['source_url'] = story['url']
    engine.store.put('stories', story)
    draft.update(demo=False, news_url=story['url'], source=story['source'],
                 crosscheck={'state': 'crosschecked', 'passed': True, 'sources': [], 'summary': 'local check'},
                 source_snapshot={'body_hash': hashlib.sha256(story['text'].encode()).hexdigest(),
                                  'published_at': now(), 'checked_at': (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(),
                                  'max_age_hours': 24, 'tier': 'secondary', 'url': story['url'], 'warnings': []})
    engine.seal_checks(draft)
    engine.store.put('drafts', draft)
    return draft


def test_unchanged_delivery_approval_and_wechat_draft_reuse_checks_without_refetch(tmp_path):
    engine = make_engine(tmp_path, fast_mode=True, review_mode='off')
    draft = checked_real_draft(engine)
    engine.wechat = FakeWechat()
    def no_network(*args, **kwargs):
        raise AssertionError('unchanged generated draft must not refetch or call an LLM')
    engine.prepare_source = no_network
    engine.verify_crosscheck = no_network
    engine.model.complete = no_network
    enable(engine, mode='auto')
    # One click approves this version without triggering the global auto-publish path.
    record = engine.submit_wechat_draft(draft['id'], 1)
    assert record['target_mode'] == 'draft'
    engine.deliver(draft['id'], 1)
    result = engine.run_publication(record['id'])
    assert result['status'] == 'draft_only'
    assert len([c for c in engine.wechat.calls if c[0] == 'draft/add']) == 1
    assert not any(c[0] == 'freepublish/submit' for c in engine.wechat.calls)
    again = engine.submit_wechat_draft(draft['id'], 1)
    assert again['id'] == record['id']
    assert again['media_id'] == result['media_id']
    assert len([c for c in engine.wechat.calls if c[0] == 'draft/add']) == 1


def test_changed_or_expired_snapshot_cannot_reuse_generation_seal(tmp_path):
    engine = make_engine(tmp_path, fast_mode=True, review_mode='off')
    draft = checked_real_draft(engine)
    def needs_fetch(*args, **kwargs):
        raise ValueError('explicit refetch reached')
    engine.prepare_source = needs_fetch
    draft['source_snapshot']['published_at'] = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    engine.seal_checks(draft)
    with pytest.raises(ValueError, match='explicit refetch'):
        engine.verify_source(draft)
    draft = engine.store.get('drafts', draft['id'])
    draft['source_snapshot']['published_at'] = now()
    engine.seal_checks(draft)
    draft['article']['digest'] += 'modified'
    draft['source_snapshot']['checked_at'] = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    with pytest.raises(ValueError, match='explicit refetch'):
        engine.verify_source(draft)


def test_one_click_never_changes_an_inflight_wechat_operation(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.publish_lock.acquire(blocking=False)
    try:
        with pytest.raises(ValueError, match='微信任务正在执行'):
            engine.submit_wechat_draft('unused', 1)
        assert not engine.store.all('publications')
    finally:
        engine.publish_lock.release()


def test_one_click_pins_unknown_draft_creation_but_never_masks_unknown_formal_publish(tmp_path):
    engine = make_engine(tmp_path, fast_mode=True, review_mode='off')
    draft = checked_real_draft(engine)
    enable(engine, mode='auto')
    record = engine.submit_wechat_draft(draft['id'], 1)
    record.pop('target_mode')
    record.update(status='unknown', intent='draft_add', attention='draft_unknown')
    engine.store.put('publications', record)
    recovered = engine.submit_wechat_draft(draft['id'], 1)
    assert recovered['status'] == 'unknown'
    assert recovered['target_mode'] == 'draft'
    assert len(engine.store.all('publications')) == 1
    recovered.update(intent='submit', attention='submit_unknown')
    engine.store.put('publications', recovered)
    with pytest.raises(ValueError, match='已进入正式发布'):
        engine.submit_wechat_draft(draft['id'], 1)
