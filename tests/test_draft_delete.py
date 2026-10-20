import copy
import json

import pytest
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.engine import Engine


def demo_draft(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path, admin_token='test'))
    engine.enqueue('pipeline')
    engine.work_once()
    draft = engine.store.all('drafts')[0]
    return engine, draft


def test_delete_draft_removes_record_exports_and_writes_event(tmp_path):
    engine, draft = demo_draft(tmp_path)
    exports = tmp_path / 'exports' / draft['id']
    assert exports.is_dir()
    result = engine.delete_draft(draft['id'], confirmed=True)
    assert result['deleted'] is True and result['title'] == draft['article']['title']
    with pytest.raises(KeyError):
        engine.store.get('drafts', draft['id'])
    assert not exports.exists()
    assert any(event['stage'] == 'deleted' for event in engine.store.events())


def test_delete_requires_confirmation_and_blocks_active_publication(tmp_path):
    engine, draft = demo_draft(tmp_path)
    with pytest.raises(ValueError, match='确认'):
        engine.delete_draft(draft['id'], confirmed=False)
    engine.store.put('publications', {'id': 'pub-1', 'draft_id': draft['id'], 'revision': draft['revision'],
                                      'status': 'accepted', 'created_at': '2026-10-20T00:00:00+00:00'})
    with pytest.raises(ValueError, match='发布记录'):
        engine.delete_draft(draft['id'], confirmed=True)
    assert engine.store.get('drafts', draft['id'])['id'] == draft['id']


def test_delete_blocks_queued_job_for_the_draft(tmp_path):
    engine, draft = demo_draft(tmp_path)
    engine.enqueue('review', draft['id'], draft['revision'])
    with pytest.raises(ValueError, match='任务'):
        engine.delete_draft(draft['id'], confirmed=True)


def test_published_draft_can_be_deleted_without_touching_platform_link(tmp_path):
    engine, draft = demo_draft(tmp_path)
    draft.update(status='published', published_url='https://mp.weixin.qq.com/s/example')
    engine.store.put('drafts', draft)
    result = engine.delete_draft(draft['id'], confirmed=True)
    assert result['published_url'] == 'https://mp.weixin.qq.com/s/example'
    with pytest.raises(KeyError):
        engine.store.get('drafts', draft['id'])


def test_delete_route_requires_confirmation(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test'), start_worker=False)
    engine = app.state.engine
    engine.enqueue('pipeline')
    engine.work_once()
    draft = engine.store.all('drafts')[0]
    headers = {'Authorization': 'Bearer test'}
    with TestClient(app) as client:
        refused = client.post(f'/api/drafts/{draft["id"]}/delete', headers=headers, json={'confirmed': False})
        assert refused.status_code == 409
        removed = client.post(f'/api/drafts/{draft["id"]}/delete', headers=headers, json={'confirmed': True})
        assert removed.status_code == 200 and removed.json()['deleted'] is True
        assert client.get('/api/state', headers=headers).json()['drafts'] == []


def test_pipeline_stops_at_requested_count_with_more_candidates(tmp_path, monkeypatch):
    from newsroom.collect import demo_items
    first = demo_items()[0]
    items = [{**copy.deepcopy(first), 'id': 'topic-%d' % index, 'title': 'Topic %d 新发布' % index,
              'url': 'https://example.com/topic-%d' % index} for index in range(5)]
    monkeypatch.setattr('newsroom.collect.demo_items', lambda: items)
    engine = Engine(Settings(data_dir=tmp_path, admin_token='test'))
    from newsroom.editors import choose_editor
    engine.enqueue('pipeline', payload={'editor': choose_editor(engine.store, None), 'article_count': 2})
    engine.work_once()
    drafts = engine.store.all('drafts')
    assert len(drafts) == 2
    run = engine.store.all('runs')[0]
    assert run['requested_count'] == 2 and '本次目标 2 篇，实际生成 2 篇' in run['message']
    assert run['status'] == 'done' and '本次目标已达成' in run['message']
    remaining = [s for s in engine.store.all('stories') if s['status'] in {'new', 'shortlisted'}]
    assert len(remaining) >= 3   # 达成目标后没有继续写剩下的候选
