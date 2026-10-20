import base64
import copy
import io
import threading

import httpx
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.engine import Engine
from newsroom.image_service import generate_image
from newsroom.models import content_hash
from newsroom.publication import PublicationAction
from newsroom.render import export_document
from newsroom.runtime_config import RuntimeConfigPatch, candidate, public_config, persist, load_saved
from newsroom.wechat import WechatAPIError, WechatUncertain
from test_publication import FakeWechat, enable, make_engine
from test_workflow_controls import checked_real_draft


def configured(tmp_path):
    engine = make_engine(tmp_path, drafts_only=True, fast_mode=True, review_mode='off')
    draft = checked_real_draft(engine)
    engine.wechat = FakeWechat()
    enable(engine, mode='auto', cover_mode='auto')
    # Real local picture fixtures, never use rasterized text to satisfy production illustration counts.
    from newsroom.images import save_source_image
    from newsroom.db import now
    assets = []
    for color in ['#315a91', '#447133']:
        raw = io.BytesIO(); Image.new('RGB', (900, 500), color).save(raw, format='PNG')
        assets.append(save_source_image(tmp_path, {'id': 'I'+color, 'origin':'article', 'source_url':draft['news_url'],
            'source_name':'local fixture', 'url':'https://cdn.example/'+color[1:]+'.png', 'caption':'实际图片'}, raw.getvalue(), 'https://cdn.example/picture.png'))
    draft['visuals'] = {'assets':assets,'article_hash':content_hash(draft['article']),'recovery_checked_at':now()}
    engine.store.put('drafts',draft)
    return engine, draft


def test_batch_builds_all_unsubmitted_with_auto_cover_no_paid_review_and_no_duplicates(tmp_path):
    engine, draft = configured(tmp_path)
    second = copy.deepcopy(draft)
    second.update(id='second-draft', author='旧署名')
    second['article']['title'] = '这一条标题非常长' * 8
    second['article']['digest'] = '过长摘要' * 40
    second['article']['markdown'] += '\n\n*本文由 AI 辅助采编与排版。*'
    second['review_hash'] = content_hash(second['article'])
    engine.store.put('drafts', second)
    engine.model.complete = lambda *a, **k: pytest.fail('draft storage must not request a text model')
    engine.prepare_source = lambda *a, **k: pytest.fail('fresh cached original must not be refetched')
    batch = engine.enqueue_draft_batch()
    assert batch['total'] == 2
    assert engine.enqueue_draft_batch()['id'] == batch['id']
    engine.work_once()
    batch = engine.store.get('draft_batches', batch['id'])
    assert batch['saved'] == 2, batch
    assert batch['failed'] == 0
    assert len([c for c in engine.wechat.calls if c[0] == 'draft/add']) == 2
    assert len([c for c in engine.wechat.calls if c[0] == 'cover/upload']) >= 1
    assert not any(c[0] == 'freepublish/submit' for c in engine.wechat.calls)
    assert engine.enqueue_draft_batch()['total'] == 0
    repaired = engine.store.get('drafts', second['id'])
    assert repaired['revision'] == 2
    assert len(repaired['article']['title']) <= 32
    assert len(repaired['article']['digest']) <= 120
    assert repaired['author'] == 'AI 编辑部'
    assert len(repaired['visuals']['assets']) >= 2
    assert repaired['visuals']['cover_asset_id']
    html = export_document(repaired)
    assert 'AI 辅助采编' not in html and 'AI 编辑部' in html
    assert all(a[1]['author'] == 'AI 编辑部' for a in engine.wechat.calls if a[0] == 'draft/add')


@pytest.mark.parametrize('error,expected_calls,expected_state', [(45003, 2, 'saved'), (48001, 1, 'failed'), ('timeout', 1, 'needs_check')])
def test_batch_only_retries_definite_fixable_rejections(tmp_path, error, expected_calls, expected_state):
    engine, draft = configured(tmp_path)
    class OnceRejected(FakeWechat):
        def add_draft(self, article):
            if not any(c[0] == 'draft/add' for c in self.calls):
                self.calls.append(('draft/add', article))
                if error == 'timeout':
                    raise WechatUncertain('result unknown')
                raise WechatAPIError(error, 'draft/add')
            return super().add_draft(article)
    engine.wechat = OnceRejected()
    batch = engine.enqueue_draft_batch()
    engine.work_once()
    result = engine.store.get('draft_batches', batch['id'])
    assert result['entries'][0]['status'] == expected_state, result
    assert len([c for c in engine.wechat.calls if c[0] == 'draft/add']) == expected_calls
    assert not any(c[0] == 'freepublish/submit' for c in engine.wechat.calls)


def test_real_app_blocks_formal_publish_even_if_stored_mode_is_auto(tmp_path):
    engine, draft = configured(tmp_path)
    record = engine.submit_wechat_draft(draft['id'], 1)
    record = engine.run_publication(record['id'])
    assert record['status'] == 'draft_only'
    with pytest.raises(ValueError, match='只建立微信草稿'):
        engine.publication_action(record['id'], 'submit', PublicationAction(confirmed=True))
    assert not any(c[0] == 'freepublish/submit' for c in engine.wechat.calls)


def test_cancel_between_upload_steps_cannot_leak_a_queued_draft_write(tmp_path):
    engine, draft = configured(tmp_path)
    batch = engine.enqueue_draft_batch()
    original = engine.wechat.upload_cover
    def upload_then_cancel(path):
        media_id = original(path)
        engine.controls.request('cancel', batch['job_id'])
        return media_id
    engine.wechat.upload_cover = upload_then_cancel
    engine.work_once()
    result = engine.store.get('draft_batches', batch['id'])
    assert result['status'] == 'cancelled'
    assert not any(c[0] == 'draft/add' for c in engine.wechat.calls)
    assert not any(j['kind'] == 'publish' and j['status'] == 'queued' for j in engine.store.all('jobs'))
    engine.publish_tick()
    assert not engine.work_once()
    assert engine.store.all('publications')[0]['status'] == 'cancelled'


def test_fast_merged_preparation_uses_two_content_requests_and_reuses_writer_cache(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path, editorial_mode='auto', fast_mode=True, merged_preparation=True, review_mode='off'))
    engine.enqueue('pipeline'); engine.work_once()
    stages = [c['stage'] for c in engine.store.all('calls')]
    assert 'prepare_article' in stages
    assert 'extract' not in stages and 'plan_article' not in stages
    assert stages.count('write') == 1
    assert not any(s in stages for s in ['review', 'visual'])


def test_real_writer_cache_avoids_a_second_paid_request_and_explicit_rewrite_bypasses_it(tmp_path, monkeypatch):
    from newsroom.llm import ModelClient
    from newsroom.collect import demo_items
    engine = Engine(Settings(data_dir=tmp_path, demo=False, llm_base_url='https://model.example/v1', llm_api_key='FAKE', llm_model='fake-model', fast_mode=True))
    run = engine.new_run('pipeline')
    context = {'news': demo_items()[0]}
    plan = ModelClient.demo('plan_article', context)
    context.update(writing_prompt=plan['writing_prompt'], editorial_plan=plan, autonomous_article=True, cache_body=True)
    requests = []
    original = httpx.Client
    def respond(request):
        import json
        requests.append(request)
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(ModelClient.demo('write', context),ensure_ascii=False)}}], 'usage':{'total_tokens':123}})
    monkeypatch.setattr('newsroom.llm.httpx.Client', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    first = engine.model.complete('write', context, run)
    assert engine.model.complete('write', context, run) == first
    assert len(requests) == 1
    assert engine.store.get('runs', run['id'])['token_usage']['total_tokens'] == 123
    engine.model.complete('write', {**context, 'cache_body':False}, run)
    assert len(requests) == 2


def test_image_configuration_is_encrypted_optional_and_never_echoes_keys(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path))
    patch = RuntimeConfigPatch(image_generation={'base_url': 'https://image.example/v1', 'model': 'example', 'api_key': 'SECRET-IMAGE', 'enabled': True}, merged_preparation=True)
    settings = candidate(engine.settings, patch)
    persist(settings)
    assert load_saved(tmp_path)['image_api_key'] == 'SECRET-IMAGE'
    assert 'SECRET-IMAGE' not in str(public_config(settings, engine.store))
    with pytest.raises(ValueError, match='重新填写'):
        candidate(settings, RuntimeConfigPatch(image_generation={'base_url': 'https://other.example/v1'}))


def test_image_generation_caches_bytes_and_does_not_retry_a_paid_failure(tmp_path, monkeypatch):
    engine, draft = configured(tmp_path)
    engine.settings.demo = False
    engine.settings.image_enabled = True
    engine.settings.image_base_url = 'https://image.example/v1'
    engine.settings.image_api_key = 'KEY'
    engine.settings.image_model = 'test-image'
    raw = io.BytesIO();Image.new('RGB', (900, 500), '#334455').save(raw, format='PNG')
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(raw.getvalue()).decode()}]})
    original = httpx.Client
    monkeypatch.setattr('newsroom.image_service.httpx.Client', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    asset = generate_image(engine, draft)
    assert asset['kind'] == 'illustration'
    assert generate_image(engine, draft)['id'] == asset['id']
    assert len(requests) == 1
    engine.settings.image_model = 'failing-image'
    def fail(request):
        requests.append(request)
        return httpx.Response(500)
    monkeypatch.setattr('newsroom.image_service.httpx.Client', lambda **kwargs: original(transport=httpx.MockTransport(fail), **kwargs))
    assert generate_image(engine, draft) is None
    assert generate_image(engine, draft) is None
    assert len(requests) == 2


def test_batch_api_requires_confirmation_and_does_not_send_requests_when_enqueued(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test', drafts_only=True), start_worker=False)
    with TestClient(app) as client:
        assert client.post('/api/drafts/wechat-batch', json={'confirmed': True}).status_code == 401
        client.headers['Authorization'] = 'Bearer test'
        assert client.post('/api/drafts/wechat-batch', json={'confirmed': False}).status_code == 409


def test_wechat_whitelist_errors_surface_the_exact_validated_ip():
    from newsroom.wechat import Wechat
    with pytest.raises(WechatAPIError, match='1.1.1.1'):
        Wechat.check({'errcode':40164,'errmsg':'invalid ip 1.1.1.1 ipv6 ::ffff:1.1.1.1, not in whitelist'}, 'stable_token')


def test_existing_failed_history_is_a_list_and_can_be_repaired_and_retried(tmp_path):
    engine, draft = configured(tmp_path)
    engine.wechat.draft_error = WechatAPIError(48001, 'draft/add')
    first = engine.enqueue_draft_batch()
    engine.work_once()
    assert engine.store.get('draft_batches', first['id'])['failed'] == 1
    record = engine.publication_for_draft(draft['id'])[0]
    assert record['errcode'] == 48001
    engine.wechat.draft_error = None
    second = engine.enqueue_draft_batch()
    engine.work_once()
    result = engine.store.get('draft_batches', second['id'])
    assert result['saved'] == 1, result
    assert engine.publication_for_draft(draft['id'])[0]['errcode'] is None
    assert engine.enqueue_draft_batch()['total'] == 0


def test_old_completed_article_stores_without_network_or_paid_recheck(tmp_path):
    from datetime import datetime, timedelta, timezone
    engine, draft = configured(tmp_path)
    draft['source_snapshot']['published_at'] = (datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    engine.seal_checks(draft)
    engine.store.put('drafts', draft)
    engine.prepare_source = lambda *a, **k: pytest.fail('storage must not refetch an old article')
    engine.model.complete = lambda *a, **k: pytest.fail('storage must not call a model')
    batch = engine.enqueue_draft_batch()
    engine.work_once()
    result = engine.store.get('draft_batches', batch['id'])
    assert result['saved'] == 1, result
    check = engine.store.get('drafts', draft['id'])['storage_check']
    assert check['outside_news_window'] and not check['online_rechecked']


def test_storage_reuses_proven_saved_page_variant_but_rejects_missing_quotes(tmp_path):
    import hashlib
    engine, draft = configured(tmp_path)
    story = engine.store.get('stories', draft['story_id'])
    # Registered pages commonly redirect the slash-normalized feed URL to its slash variant.
    draft['source_snapshot']['url'] = story['url'].rstrip('/')
    story['text'] += '\nAn advertisement changed.'
    story['source_snapshot'] = {**draft['source_snapshot'], 'body_hash': hashlib.sha256(story['text'].encode()).hexdigest()}
    engine.store.put('stories', story)
    engine.verify_source(draft)
    assert draft['storage_check']['equivalent_saved_page']
    story['text'] = 'An entirely replaced story.'
    story['source_snapshot']['body_hash'] = hashlib.sha256(story['text'].encode()).hexdigest()
    engine.store.put('stories', story)
    with pytest.raises(ValueError, match='快照缺失或已改变'):
        engine.verify_source(draft)


def test_whitelist_error_stops_shared_batch_without_attempting_remaining_articles(tmp_path):
    engine, draft = configured(tmp_path)
    second = copy.deepcopy(draft)
    second['id'] = 'second'
    engine.store.put('drafts', second)
    class BlockedAccount(FakeWechat):
        def upload_cover(self, path):
            self.calls.append(('cover/upload', str(path)))
            raise WechatAPIError(40164, 'stable_token', whitelist_ip='9.9.9.9')
    engine.wechat = BlockedAccount()
    batch = engine.enqueue_draft_batch()
    engine.work_once()
    result = engine.store.get('draft_batches', batch['id'])
    assert result['status'] == 'blocked', result
    assert result['failed'] == 1 and result['blocked'] == 1
    assert result['blocker']['errcode'] == 40164
    assert '9.9.9.9' in result['blocker']['error']
    assert len(engine.wechat.calls) == 1
    assert not any(c[0] == 'draft/add' for c in engine.wechat.calls)


def test_cancelled_history_with_media_id_is_not_duplicated(tmp_path):
    engine, draft = configured(tmp_path)
    engine.store.put('publications', {'id': 'legacy', 'draft_id': draft['id'], 'revision': draft['revision'],
        'account': engine.settings.wechat_app_id, 'status': 'cancelled', 'media_id': 'OLD-MEDIA',
        'intent': '', 'created_at': '2026-10-19'})
    batch = engine.enqueue_draft_batch()
    engine.work_once()
    result = engine.store.get('draft_batches', batch['id'])
    assert result['needs_check'] == 1 and result['failed'] == 0
    assert engine.wechat.calls == []


def test_failed_local_preparation_does_not_persist_half_repaired_article(tmp_path):
    engine, draft = configured(tmp_path)
    draft['article']['title'] = '这是尚未完成适配的原标题' * 5
    draft['review_hash'] = content_hash(draft['article'])
    engine.store.put('drafts', draft)
    def fail_visuals(*a, **k):
        raise ValueError('local image validation failed')
    engine.prepare_visuals = fail_visuals
    batch = engine.enqueue_draft_batch()
    engine.work_once()
    assert engine.store.get('draft_batches', batch['id'])['failed'] == 1
    stored = engine.store.get('drafts', draft['id'])
    assert stored['article'] == draft['article'] and stored['revision'] == draft['revision']
    assert engine.wechat.calls == []
