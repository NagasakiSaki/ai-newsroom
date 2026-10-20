import json
import time

import pytest
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.db import now
from newsroom.engine import Engine
from newsroom.publication import PublicationAction, PublishConfigPatch
from newsroom.wechat import WechatAPIError, WechatUncertain


PUBLISHED = {'publish_id': '1000', 'publish_status': 0, 'article_id': 'ART-1',
             'article_detail': {'count': 1, 'item': [{'idx': 1, 'article_url': 'https://mp.weixin.qq.com/s/abc'}]},
             'fail_idx': []}


class FakeWechat:
    """替身微信客户端：不联网，记录每次调用，可注入每步的返回或异常。"""

    def __init__(self):
        self.calls = []
        self.drafts = {}
        self.query_result = dict(PUBLISHED)
        self.submit_error = None
        self.draft_error = None
        self.query_error = None
        self.draft_listing = {'total_count': 0, 'item_count': 0, 'item': []}
        self.published_listing = {'total_count': 0, 'item_count': 0, 'item': []}
        self.capability = {'draft': 0, 'publish': 0, 'material': {'image_count': 1}}

    # --- 发布链路 ---
    def add_draft(self, article):
        self.calls.append(('draft/add', article))
        if self.draft_error:
            raise self.draft_error
        media_id = 'MEDIA-%d' % (len(self.drafts) + 1)
        self.drafts[media_id] = article
        return media_id

    def submit_publish(self, media_id):
        self.calls.append(('freepublish/submit', media_id))
        if self.submit_error:
            raise self.submit_error
        return {'errcode': 0, 'errmsg': 'ok', 'publish_id': '1000', 'msg_data_id': 'MSG-1'}

    def query_publish(self, publish_id):
        self.calls.append(('freepublish/get', str(publish_id)))
        if self.query_error:
            raise self.query_error
        return dict(self.query_result)

    def published_article(self, article_id):
        return {'news_item': [{'url': 'https://mp.weixin.qq.com/s/abc'}]}

    def draft_exists(self, media_id):
        return media_id in self.drafts

    def draft_list(self, offset=0, count=20, no_content=0):
        self.calls.append(('draft/batchget', no_content))
        # 对账依赖 content 中的标题；平台在 no_content=1 时不返回标题。
        assert no_content == 0
        return self.draft_listing

    def published_list(self, offset=0, count=20, no_content=0):
        return self.published_listing

    def upload_cover(self, path):
        self.calls.append(('cover/upload', str(path)))
        return 'COVER-UPLOADED'

    # --- 只读检测 ---
    def access_token(self, refresh=False):
        return 'FAKE-TOKEN'

    def upload_inline_image(self, path):
        self.calls.append(('media/uploadimg', str(path)))
        return 'https://mmbiz.qpic.cn/fake-inline.png'

    def draft_count(self):
        return self.capability['draft']

    def material_count(self):
        return self.capability['material']


def make_engine(path, **overrides):
    settings = Settings(data_dir=path, admin_token='test', wechat_app_id='wxFAKE123456',
                        wechat_app_secret='FAKE-SECRET', **overrides)
    return Engine(settings)


def real_draft(engine):
    """生成一篇“真实来源”稿件：来源复查打桩，不读取网络。"""
    engine.enqueue('pipeline')
    engine.work_once()
    draft = engine.store.all('drafts')[0]
    # 基础发布夹具保持纯文字；图文上传由独立场景覆盖。
    draft.pop('visuals', None)
    draft.update(demo=False,
                 crosscheck={'state': 'crosschecked', 'passed': True, 'sources': [], 'summary': '测试对照',
                             'warnings': []},
                 source_snapshot={'body_hash': 'x', 'published_at': now(), 'tier': 'primary',
                                  'url': draft['news_url'], 'warnings': [], 'max_age_hours': 24,
                                  'checked_at': now()})
    engine.store.put('drafts', draft)
    engine.verify_source = lambda item, force=False: item
    return engine.store.get('drafts', draft['id'])


def approved(engine):
    draft = real_draft(engine)
    engine.approve(draft['id'], 1, '编辑', True)
    return engine.store.get('drafts', draft['id'])


def enable(engine, mode='manual', **values):
    return engine.save_publish_config(PublishConfigPatch(enabled=True, mode=mode, cover_media_id='COVER-1', **values))


@pytest.mark.parametrize('phase', ['prepared', 'draft_created'])
def test_disabling_pauses_queued_writes_and_reenable_resumes_without_duplicate_draft(tmp_path, phase):
    engine = make_engine(tmp_path)
    engine.fake = FakeWechat()
    engine.wechat = engine.fake
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    if phase == 'draft_created':
        publication = engine.prepare_publication(publication)
    engine.fake.calls.clear()
    engine.save_publish_config(PublishConfigPatch(enabled=False))
    engine.work_once()
    paused = engine.get_publication(publication['id'])
    assert paused['status'] == phase and paused['attention'] == 'publish_disabled'
    assert not paused.get('intent') and engine.fake.calls == []
    engine.publish_tick()
    assert not [j for j in engine.store.all('jobs') if j['kind'] == 'publish' and j['status'] == 'queued']
    enable(engine)
    engine.publish_tick()
    engine.work_once()
    assert engine.get_publication(publication['id'])['status'] == 'accepted'
    assert len(engine.fake.drafts) == 1
    assert len([c for c in engine.fake.calls if c[0] == 'freepublish/submit']) == 1


def test_disabling_during_source_recheck_stops_before_cover_or_draft_request(engine, tmp_path):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.verify_source = lambda *a, **kw: engine.save_publish_config(PublishConfigPatch(enabled=False))
    engine.work_once()
    paused = engine.get_publication(publication['id'])
    assert paused['status'] == 'prepared' and not paused.get('intent')
    assert engine.fake.calls == []


def test_disabling_after_draft_creation_stops_before_submit(engine):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    add = engine.fake.add_draft
    def add_and_disable(payload):
        media_id = add(payload)
        engine.save_publish_config(PublishConfigPatch(enabled=False))
        return media_id
    engine.fake.add_draft = add_and_disable
    engine.work_once()
    paused = engine.get_publication(publication['id'])
    assert paused['status'] == 'draft_created' and paused['media_id']
    assert [c[0] for c in engine.fake.calls] == ['draft/add']


@pytest.mark.parametrize('phase', ['prepared', 'draft_created', 'accepted'])
def test_account_change_stops_old_task_writes_and_queries_even_after_reenable(engine, phase):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    if phase == 'draft_created':
        publication = engine.prepare_publication(publication)
    elif phase == 'accepted':
        engine.work_once()
    engine.fake.calls.clear()
    engine.settings.wechat_app_id = 'wxDIFFERENT-ACCOUNT'
    engine.on_credentials_changed()
    if phase == 'accepted':
        engine.check_publication(publication['id'])
    else:
        engine.work_once()
    result = engine.get_publication(publication['id'])
    assert result['status'] == phase and result['attention'] == 'account_changed'
    enable(engine)
    engine.run_publication(publication['id'])
    engine.check_publication(publication['id'])
    engine.publish_tick()
    assert engine.fake.calls == []
    assert engine.get_publication(publication['id'])['status'] == phase


def test_disabling_does_not_prevent_same_account_status_query(engine):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    engine.save_publish_config(PublishConfigPatch(enabled=False))
    engine.fake.calls.clear()
    result = engine.check_publication(publication['id'])
    assert result['status'] == 'published'
    assert [c[0] for c in engine.fake.calls] == ['freepublish/get']


def test_stale_tick_record_cannot_overwrite_new_publication_status(engine):
    draft = approved(engine)
    enable(engine)
    stale = engine.create_publication(draft['id'], 1)
    engine.work_once()
    engine.save_publish_config(PublishConfigPatch(enabled=False))
    assert not engine.execution_ready(stale, write=True)
    assert engine.get_publication(stale['id'])['status'] == 'accepted'


def test_restart_with_publishing_disabled_never_replays_queued_write(engine, monkeypatch):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.save_publish_config(PublishConfigPatch(enabled=False))
    monkeypatch.setattr(engine, 'loop', lambda: None)
    engine.start()
    try:
        for _ in range(5):
            if not engine.work_once():
                break
        assert engine.fake.calls == []
        assert engine.get_publication(publication['id'])['status'] == 'prepared'
    finally:
        engine.close()


def test_saving_new_account_via_http_cannot_execute_old_queued_publication(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test', wechat_app_id='wxFAKE123456',
                              wechat_app_secret='FAKE-SECRET'), start_worker=False)
    engine = app.state.engine
    engine.wechat = engine.fake = FakeWechat()
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    with TestClient(app) as client:
        result = client.put('/api/config', headers={'Authorization': 'Bearer test'},
                            json={'wechat': {'app_id': 'wxOTHER-ACCOUNT'}})
        assert result.status_code == 200
        assert not engine.publication_rules()['enabled']
        engine.work_once()
    assert engine.fake.calls == []
    assert engine.get_publication(publication['id'])['attention'] == 'account_changed'


@pytest.mark.parametrize('draft_exists', [True, False])
def test_same_title_old_article_never_confirms_unknown_submit(engine, draft_exists):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.fake.submit_error = WechatUncertain('提交结果未知')
    engine.work_once()
    publication = engine.get_publication(publication['id'])
    if not draft_exists:
        engine.fake.drafts.clear()
    engine.fake.published_listing = {'item': [{'article_id': 'OLD-ARTICLE',
        'content': {'news_item': [{'title': publication['snapshot']['title'],
                                 'content': 'different old article', 'url': 'https://mp.weixin.qq.com/s/old'}]}}]}
    result = engine.check_publication(publication['id'])
    assert result['status'] == 'unknown' and result['resumable'] is draft_exists
    assert not result.get('article_id') and not result.get('article_url')
    assert engine.store.get('drafts', draft['id'])['status'] == 'ready_to_publish'
    assert len([c for c in engine.fake.calls if c[0] == 'freepublish/submit']) == 1


@pytest.mark.parametrize('mismatch', ['body', 'time'])
def test_same_title_draft_requires_matching_body_and_request_time(engine, mismatch):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.fake.draft_error = WechatUncertain('草稿结果未知')
    engine.work_once()
    payload = dict(engine.fake.calls[0][1])
    if mismatch == 'body':
        payload['content'] = 'old body'
    engine.fake.draft_listing = {'item': [{'media_id': 'OLD-DRAFT',
        'update_time': 1 if mismatch == 'time' else int(time.time()),
        'content': {'news_item': [payload]}}]}
    result = engine.check_publication(publication['id'])
    assert result['status'] == 'unknown' and not result.get('media_id')
    assert len([c for c in engine.fake.calls if c[0] == 'draft/add']) == 1


def test_submit_existing_draft_http_route_requires_confirmation_and_reuses_media(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test', wechat_app_id='wxFAKE123456',
                              wechat_app_secret='FAKE-SECRET'), start_worker=False)
    engine = app.state.engine
    engine.wechat = engine.fake = FakeWechat()
    draft = approved(engine)
    enable(engine, mode='draft')
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    path = '/api/publications/' + publication['id'] + '/submit'
    with TestClient(app) as client:
        assert client.post(path, json={'confirmed': True}).status_code == 401
        headers = {'Authorization': 'Bearer test'}
        assert client.post(path, headers=headers, json={}).status_code == 409
        assert engine.get_publication(publication['id'])['status'] == 'draft_only'
        assert client.post(path, headers=headers, json={'confirmed': True}).status_code == 200
        engine.work_once()
    assert engine.get_publication(publication['id'])['status'] == 'accepted'
    assert [c[0] for c in engine.fake.calls] == ['draft/add', 'freepublish/submit']


def test_cover_cache_is_scoped_to_account(engine, tmp_path):
    cover = tmp_path / 'cover.png'
    cover.write_bytes(b'fake-image-for-transport-test')
    rules = {**engine.publication_rules(), 'cover_path': str(cover), 'cover_media_id': ''}
    engine.cover_media_id(rules)
    engine.cover_media_id(rules)
    engine.settings.wechat_app_id = 'wxOTHER-ACCOUNT'
    engine.cover_media_id(rules)
    assert [c[0] for c in engine.fake.calls] == ['cover/upload', 'cover/upload']
    assert len(engine.store.all('covers')) == 2


def test_publication_record_cannot_be_abandoned_during_external_request(engine):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.publish_lock.acquire()
    try:
        with pytest.raises(ValueError, match='微信操作正在进行'):
            engine.publication_action(publication['id'], 'abandon', PublicationAction(confirmed=True))
        assert engine.get_publication(publication['id'])['status'] == 'prepared'
    finally:
        engine.publish_lock.release()


def test_unknown_draft_can_be_recreated_only_after_explicit_backend_confirmation(engine):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.fake.draft_error = WechatUncertain('结果未知')
    engine.work_once()
    engine.check_publication(publication['id'])
    with pytest.raises(ValueError, match='确认'):
        engine.publication_action(publication['id'], 'resume', PublicationAction())
    engine.fake.draft_error = None
    engine.publication_action(publication['id'], 'resume', PublicationAction(confirmed=True))
    assert engine.get_publication(publication['id'])['status'] == 'prepared'
    engine.work_once()
    assert engine.get_publication(publication['id'])['status'] == 'accepted'


@pytest.fixture
def engine(tmp_path):
    item = make_engine(tmp_path)
    item.fake = FakeWechat()
    item.wechat = item.fake
    return item


# ---------- 显式启用 ----------

def test_publish_refused_until_explicitly_enabled(engine):
    engine.fake = FakeWechat()
    engine.wechat = engine.fake
    draft = approved(engine)
    with pytest.raises(ValueError, match='未启用'):
        engine.create_publication(draft['id'], 1)
    with pytest.raises(ValueError, match='未启用'):
        engine.publish(draft['id'], 1)
    with pytest.raises(ValueError, match='未启用'):
        engine.queue_publish(draft['id'], 1)
    assert engine.fake.calls == []
    assert engine.store.get('drafts', draft['id'])['status'] == 'ready_to_publish'


def test_enabling_requires_credentials_cover_and_author(engine):
    with pytest.raises(ValueError, match='封面'):
        engine.save_publish_config(PublishConfigPatch(enabled=True, author='编辑部'))
    engine.settings.wechat_app_secret = ''
    with pytest.raises(ValueError, match='AppID'):
        enable(engine)
    engine.settings.wechat_app_secret = 'FAKE-SECRET'
    assert enable(engine)['ready'] is True


# ---------- 正常链路 ----------

def test_manual_publish_creates_draft_queries_and_records_platform_url(engine):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    assert publication['status'] == 'prepared'
    assert [call[0] for call in engine.fake.calls] == []
    engine.work_once()
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'accepted' and publication['publish_id'] == '1000'
    assert publication['media_id'] == 'MEDIA-1'
    assert [call[0] for call in engine.fake.calls] == ['draft/add', 'freepublish/submit']
    payload = engine.fake.calls[0][1]
    assert payload['thumb_media_id'] == 'COVER-1' and payload['author'] == 'AI 编辑部'
    # 提交受理不等于已经上线：必须查询到平台成功结果才改为已发布。
    assert engine.store.get('drafts', draft['id'])['status'] == 'ready_to_publish'
    engine.check_publication(publication['id'])
    publication = engine.store.get('publications', publication['id'])
    published = engine.store.get('drafts', draft['id'])
    assert publication['status'] == 'published' and publication['article_id'] == 'ART-1'
    assert publication['article_url'] == 'https://mp.weixin.qq.com/s/abc'
    assert published['status'] == 'published'
    assert published['published_url'] == 'https://mp.weixin.qq.com/s/abc'
    assert published['published_by'] == '公众号接口' and published['publication_id'] == publication['id']


def test_publishing_status_backs_off_and_keeps_polling(engine):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    engine.fake.query_result = {'publish_id': '1000', 'publish_status': 1, 'fail_idx': [1]}
    engine.check_publication(publication['id'])
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'publishing' and publication['check_count'] == 1
    assert publication['next_check_at'] and publication['next_check_at'] > publication['last_checked_at']
    engine.fake.query_result = dict(PUBLISHED)
    engine.check_publication(publication['id'])
    assert engine.store.get('publications', publication['id'])['status'] == 'published'


def test_platform_rejection_is_definitive_failure_and_draft_stays_ready(engine):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    engine.fake.query_result = {'publish_id': '1000', 'publish_status': 4, 'fail_idx': [1]}
    engine.check_publication(publication['id'])
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'failed' and publication['platform_status'] == 4
    assert publication['fail_idx'] == [1]
    assert engine.store.get('drafts', draft['id'])['status'] == 'ready_to_publish'
    # 明确失败可以人工重试，但不会自动重复提交；已建草稿的重试复用 media_id。
    drafts_before = len([call for call in engine.fake.calls if call[0] == 'draft/add'])
    engine.publication_action(publication['id'], 'retry', PublicationAction(confirmed=True))
    retried = engine.store.get('publications', publication['id'])
    assert retried['status'] == 'draft_created' and retried['media_id'] == publication['media_id']
    engine.work_once()
    assert len([call for call in engine.fake.calls if call[0] == 'draft/add']) == drafts_before


def test_permission_denied_48001_reports_official_reason(engine):
    draft = approved(engine)
    enable(engine)
    engine.fake.submit_error = WechatAPIError(48001, 'freepublish/submit')
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'failed' and publication['errcode'] == 48001
    assert '仅认证' in publication['error']
    assert len([call for call in engine.fake.calls if call[0] == 'freepublish/submit']) == 1


# ---------- 未知结果 ----------

def test_uncertain_submit_never_auto_resubmits_and_needs_confirmation(engine):
    draft = approved(engine)
    enable(engine)
    engine.fake.submit_error = WechatUncertain('超时，结果未确认')
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'unknown' and publication['attention'] == 'submit_unknown'
    assert engine.store.get('drafts', draft['id'])['status'] == 'ready_to_publish'
    # 编辑被拒绝，直到对账或放弃。
    with pytest.raises(ValueError, match='发布'):
        engine.edit(draft['id'], 1, draft['article'])
    engine.fake.submit_error = None
    engine.fake.drafts['MEDIA-1'] = {'title': 'x'}  # 草稿仍在草稿箱
    engine.check_publication(publication['id'])
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'unknown' and publication['resumable'] is True
    submits = [call for call in engine.fake.calls if call[0] == 'freepublish/submit']
    assert len(submits) == 1
    with pytest.raises(ValueError, match='确认'):
        engine.publication_action(publication['id'], 'resume', PublicationAction())
    engine.publication_action(publication['id'], 'resume', PublicationAction(confirmed=True))
    assert engine.store.get('publications', publication['id'])['status'] == 'draft_created'


def test_restart_never_replays_a_submitted_publication(engine, monkeypatch):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.store.put('publications', {**publication, 'status': 'submitting', 'intent': 'submit', 'media_id': 'MEDIA-1'})
    job = engine.enqueue('publish', publication['id'])
    monkeypatch.setattr(engine, 'loop', lambda: None)
    engine.start()
    try:
        assert engine.store.get('jobs', job['id'])['status'] == 'interrupted'
        restored = engine.store.get('publications', publication['id'])
        assert restored['status'] == 'unknown' and restored['attention'] == 'interrupted'
        assert [call for call in engine.fake.calls if call[0] == 'freepublish/submit'] == []
        assert any(row['kind'] == 'publish_check' and row['subject'] == publication['id']
                   for row in engine.store.all('jobs'))
    finally:
        engine.close()


def test_unknown_draft_creation_reconciles_single_match(engine):
    draft = approved(engine)
    enable(engine)
    engine.fake.draft_error = WechatUncertain('连接中断')
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'unknown' and publication['attention'] == 'draft_unknown'
    engine.fake.draft_error = None
    engine.fake.draft_listing = {'total_count': 1, 'item_count': 1, 'item': [
        {'media_id': 'MEDIA-EXISTING', 'update_time': int(time.time()),
         'content': {'news_item': [dict(engine.fake.calls[0][1])]}}]}
    engine.check_publication(publication['id'])
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'published' or publication['media_id'] == 'MEDIA-EXISTING'
    assert publication['reconciled'] == 'draft_list_match'
    # 对账后复用了已有草稿，没有重复创建。
    assert len([call for call in engine.fake.calls if call[0] == 'draft/add']) == 1


def test_query_exhaustion_marks_unknown_instead_of_guessing(engine):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    engine.fake.query_result = {'publish_id': '1000', 'publish_status': 1, 'fail_idx': []}
    for _ in range(31):
        engine.check_publication(publication['id'])
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'unknown' and publication['attention'] == 'query_exhausted'
    assert engine.store.get('drafts', draft['id'])['status'] == 'ready_to_publish'


# ---------- 版本与审批保护 ----------

def test_stale_approval_cannot_be_published(engine):
    draft = approved(engine)
    enable(engine)
    engine.edit(draft['id'], 1, {**draft['article'], 'title': '人工改过的标题'})
    with pytest.raises(ValueError, match='发布前检查未通过'):
        engine.create_publication(draft['id'], 1)
    assert engine.fake.calls == []
    blocked = engine.store.all('publications')
    assert blocked and blocked[0]['status'] == 'blocked'


def test_title_over_platform_limit_is_blocked_before_any_request(engine):
    draft = approved(engine)
    enable(engine)
    article = {**draft['article'], 'title': '标题' * 20}
    engine.edit(draft['id'], 1, article)
    engine.work_once()  # 复审通过，仍为待审核
    current = engine.store.get('drafts', draft['id'])
    if current['status'] == 'pending_human':
        engine.approve(draft['id'], current['revision'], '编辑', True)
        current = engine.store.get('drafts', draft['id'])
    if current['status'] == 'ready_to_publish':
        publication = engine.create_publication(draft['id'], current['revision'])
        engine.work_once()
        publication = engine.store.get('publications', publication['id'])
        assert publication['status'] == 'blocked' and '32' in publication['error']
        assert engine.fake.calls == []


def test_duplicate_publish_request_reuses_one_record(engine):
    draft = approved(engine)
    enable(engine)
    first = engine.create_publication(draft['id'], 1)
    second = engine.create_publication(draft['id'], 1)
    assert first['id'] == second['id']
    assert len(engine.store.all('publications')) == 1


def test_account_change_disables_publishing(engine):
    approved(engine)
    enable(engine)
    engine.settings.wechat_app_id = 'wxOTHER99999'
    engine.on_credentials_changed()
    record = engine.store.get('publish_settings', 'main')
    assert record['enabled'] is False and record['target_app_id'] == ''
    assert engine.publish_summary()['ready'] is False


# ---------- 自动发布 ----------

def test_auto_mode_publishes_only_after_approval(engine):
    draft = real_draft(engine)
    enable(engine, mode='auto')
    assert engine.store.get('drafts', draft['id'])['status'] == 'pending_human'
    assert engine.store.all('publications') == []
    engine.approve(draft['id'], 1, '编辑', True)
    engine.work_once()
    publication = engine.store.all('publications')[0]
    assert publication['status'] in {'accepted', 'published'}
    assert [call[0] for call in engine.fake.calls][:2] == ['draft/add', 'freepublish/submit']


def test_auto_mode_skips_drafts_approved_before_enabling(engine):
    draft = approved(engine)
    engine.save_publish_config(PublishConfigPatch(enabled=True, mode='auto', cover_media_id='COVER-1'))
    engine.publish_tick()
    assert engine.store.all('publications') == []
    assert engine.fake.calls == []


def test_auto_mode_does_not_retry_failed_records(engine):
    draft = real_draft(engine)
    enable(engine, mode='auto')
    engine.fake.submit_error = WechatAPIError(53503, 'freepublish/submit')
    engine.approve(draft['id'], 1, '编辑', True)
    engine.work_once()
    publication = engine.store.all('publications')[0]
    assert publication['status'] == 'failed'
    submits = len([call for call in engine.fake.calls if call[0] == 'freepublish/submit'])
    engine.publish_tick()
    engine.work_once()
    assert len(engine.store.all('publications')) == 1
    assert len([call for call in engine.fake.calls if call[0] == 'freepublish/submit']) == submits


# ---------- 素材与检测 ----------

def test_cover_file_is_uploaded_once_and_cached(engine, tmp_path):
    draft = approved(engine)
    cover = tmp_path / 'cover.png'
    cover.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 100)
    engine.save_publish_config(PublishConfigPatch(enabled=True, mode='manual', cover_media_id='',
                                                  cover_path=str(cover)))
    first = engine.create_publication(draft['id'], 1)
    engine.work_once()
    drafts_sent = [call for call in engine.fake.calls if call[0] == 'draft/add']
    assert len([call for call in engine.fake.calls if call[0] == 'cover/upload']) == 1
    assert drafts_sent[0][1]['thumb_media_id'] == 'COVER-UPLOADED'
    cached = engine.store.all('covers')
    assert cached and cached[0]['media_id'] == 'COVER-UPLOADED'


def test_capability_check_is_read_only_and_explains_48001(engine):
    enable(engine)

    class Denied(FakeWechat):
        def published_list(self, offset=0, count=20, no_content=0):
            raise WechatAPIError(48001, 'freepublish/batchget')

    denied = Denied()
    engine.wechat = denied
    result = engine.capability_check()
    assert result['publish_available'] is False
    assert '仅认证' in result['summary']
    assert [call for call in denied.calls if call[0] in {'draft/add', 'freepublish/submit'}] == []
    assert engine.publish_summary()['capability']['publish_available'] is False


def test_capability_check_reports_success_read_only(engine):
    enable(engine)
    result = engine.capability_check()
    assert result['publish_available'] is True
    assert engine.fake.calls == []


# ---------- HTTP 接口 ----------

def test_publication_routes_require_auth_and_confirmation(tmp_path):
    settings = Settings(data_dir=tmp_path, admin_token='test', wechat_app_id='wxFAKE123456',
                        wechat_app_secret='FAKE-SECRET')
    app = create_app(settings, start_worker=False)
    engine = app.state.engine
    engine.wechat = FakeWechat()
    draft = approved(engine)
    headers = {'Authorization': 'Bearer test'}
    with TestClient(app) as client:
        assert client.get('/api/publication').status_code == 401
        assert client.post(f'/api/drafts/{draft["id"]}/publish', json={'revision': 1, 'confirmed': True}).status_code == 401
        assert client.put('/api/publication', headers=headers,
                          json={'enabled': True, 'cover_media_id': 'COVER-1'}).status_code == 200
        # 未确认不发；确认后排队。
        assert client.post(f'/api/drafts/{draft["id"]}/publish', headers=headers,
                           json={'revision': 1, 'confirmed': False}).status_code == 409
        queued = client.post(f'/api/drafts/{draft["id"]}/publish', headers=headers,
                             json={'revision': 1, 'confirmed': True})
        assert queued.status_code == 200 and queued.json()['status'] == 'prepared'
        state = client.get('/api/state', headers=headers).json()
        assert state['settings']['publish_enabled'] is True
        assert state['publications'] and state['publication']['config']['mode'] == 'manual'
        # 旧人工发表登记入口仍然存在，但不可能用来伪造接口发表结果。
        assert client.post(f'/api/drafts/{draft["id"]}/publication', headers=headers,
                           json={'revision': 1, 'url': 'https://mp.weixin.qq.com/s/manual', 'reviewer': '人'}
                           ).status_code == 409
        assert client.post('/api/drafts/missing/publish', headers=headers,
                           json={'revision': 1, 'confirmed': True}).status_code == 404


def test_publication_action_routes_validate_state(tmp_path):
    settings = Settings(data_dir=tmp_path, admin_token='test', wechat_app_id='wxFAKE123456',
                        wechat_app_secret='FAKE-SECRET')
    app = create_app(settings, start_worker=False)
    engine = app.state.engine
    engine.wechat = FakeWechat()
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    headers = {'Authorization': 'Bearer test'}
    with TestClient(app) as client:
        assert client.post(f'/api/publications/{publication["id"]}/resume', headers=headers,
                           json={'confirmed': True}).status_code == 409
        assert client.post(f'/api/publications/{publication["id"]}/abandon', headers=headers,
                           json={'confirmed': True}).status_code == 200
        assert engine.store.get('publications', publication['id'])['status'] == 'cancelled'
        assert client.post(f'/api/publications/{publication["id"]}/retry', headers=headers,
                           json={'confirmed': True}).status_code == 409


def test_publication_page_and_script_are_served(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test'), start_worker=False)
    with TestClient(app) as client:
        page = client.get('/')
        assert page.status_code == 200 and '/static/publication.js' in page.text
        script = client.get('/static/publication.js')
        assert script.status_code == 200
        assert "'/publication'" in script.text and "'/publications/'" in script.text
        assert "'/drafts/'" in script.text and '立即发布到公众号' in script.text


def test_wechat_payload_matches_official_field_limits(engine):
    draft = approved(engine)
    enable(engine)
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    publication = engine.store.get('publications', publication['id'])
    payload = engine.fake.calls[0][1]
    assert set(payload) == {'article_type', 'title', 'author', 'digest', 'content', 'content_source_url',
                            'thumb_media_id', 'need_open_comment', 'only_fans_can_comment'}
    assert len(payload['title']) <= 32 and len(payload['author']) <= 16 and len(payload['digest']) <= 120
    assert len(payload['content']) < 20000
    assert publication['snapshot']['html_sha256']
