import httpx
import pytest
from contextlib import contextmanager

from newsroom.collect import fetch_document
from newsroom.config import Settings
from newsroom.engine import Engine
from newsroom.editors import choose_editor
from newsroom.publication import PublishConfigPatch, PublicationAction


def make(tmp_path):
    return Engine(Settings(data_dir=tmp_path, admin_token='test',
                          wechat_app_id='wxFAKE123456', wechat_app_secret='FAKE-SECRET'))


def test_auto_approve_is_off_by_default_and_skips_demo(tmp_path):
    engine = make(tmp_path)
    engine.enqueue('pipeline')
    engine.work_once()
    demo = engine.store.all('drafts')[0]
    assert demo['status'] == 'pending_human' and demo['demo']
    assert engine.auto_approve(demo['id'], 1) is None
    # 即使打开开关，演示稿也不会被自动审定成真实发表依据。
    engine.save_publish_config(PublishConfigPatch(auto_approve=True))
    assert engine.auto_approve(demo['id'], 1) is None
    assert engine.store.get('drafts', demo['id'])['status'] == 'pending_human'


def test_auto_approve_marks_ai_review_when_enabled(tmp_path):
    from tests.test_publication import real_draft
    engine = make(tmp_path)
    draft = real_draft(engine)
    engine.save_publish_config(PublishConfigPatch(auto_approve=True))
    result = engine.auto_approve(draft['id'], 1)
    assert result['status'] == 'ready_to_publish'
    assert result['approved_by'] == 'AI 自动终审（未人工复核）'
    assert result['manual_publication'] is False
    assert result['approved_hash'] == engine.final_hash(result)


def test_publish_mode_draft_only_creates_draft_without_submitting(tmp_path):
    from tests.test_publication import FakeWechat, approved
    engine = make(tmp_path)
    engine.fake = FakeWechat()
    engine.wechat = engine.fake
    draft = approved(engine)
    engine.save_publish_config(PublishConfigPatch(enabled=True, mode='draft', cover_media_id='COVER-1'))
    publication = engine.create_publication(draft['id'], 1)
    engine.work_once()
    publication = engine.store.get('publications', publication['id'])
    assert publication['status'] == 'draft_only' and publication['media_id'] == 'MEDIA-1'
    assert [call[0] for call in engine.fake.calls] == ['draft/add']
    # 人工在后台确认后可以补交提交发布，复用同一 media_id。
    engine.publication_action(publication['id'], 'submit', PublicationAction(confirmed=True))
    assert engine.store.get('publications', publication['id'])['status'] == 'draft_created'
    engine.work_once()
    assert [call[0] for call in engine.fake.calls] == ['draft/add', 'freepublish/submit']


def test_commentary_format_is_stored_with_draft(tmp_path):
    engine = make(tmp_path)
    editor = choose_editor(engine.store, {'format': 'commentary'})
    assert editor['format'] == 'commentary'
    engine.enqueue('pipeline', payload={'editor': editor})
    engine.work_once()
    draft = engine.store.all('drafts')[0]
    assert draft['format'] == 'commentary'
    assert draft['editor']['format'] == 'commentary'


def test_news_fetch_falls_back_to_direct_when_proxy_is_dead(monkeypatch):
    seen = []

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            seen.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @contextmanager
        def stream(self, method, url):
            if 'proxy' in self.kwargs:
                raise httpx.ConnectError('proxy is down')
            yield httpx.Response(200, content=b'<article>ok</article>', request=httpx.Request(method, url))

    monkeypatch.setattr('newsroom.collect.httpx.Client', Client)
    monkeypatch.setattr('newsroom.collect.public_url', lambda url, **kw: url)
    settings = Settings(data_dir='/tmp', admin_token='test', news_proxy_url='http://127.0.0.1:7897')
    result = fetch_document('https://openai.com/news/test', settings=settings)
    assert 'ok' in result['html'] and result['proxy_fallback'] is True
    assert seen[0].get('proxy') == 'http://127.0.0.1:7897'
    assert 'proxy' not in seen[1]


def test_news_fetch_does_not_fall_back_without_proxy(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @contextmanager
        def stream(self, method, url):
            raise httpx.ConnectError('direct is down')
            yield

    monkeypatch.setattr('newsroom.collect.httpx.Client', Client)
    monkeypatch.setattr('newsroom.collect.public_url', lambda url, **kw: url)
    settings = Settings(data_dir='/tmp', admin_token='test')
    with pytest.raises(httpx.ConnectError):
        fetch_document('https://openai.com/news/test', settings=settings)


def test_legacy_publish_settings_record_is_backfilled(tmp_path):
    engine = make(tmp_path)
    engine.store.put('publish_settings', {'id': 'main', 'enabled': False, 'mode': 'manual',
                                          'author': 'AI 编辑部', 'cover_media_id': '', 'cover_path': '',
                                          'need_open_comment': 0, 'only_fans_can_comment': 0,
                                          'target_app_id': '', 'auto_since': '', 'updated_at': ''})
    rules = engine.publication_rules()
    assert rules['auto_approve'] is False and rules['mode'] == 'manual'
    engine.publish_tick()          # 不能抛异常
    assert engine.store.get('publish_settings', 'main')['auto_approve'] is False


def test_broken_publish_settings_falls_back_to_defaults(tmp_path):
    engine = make(tmp_path)
    engine.store.put('publish_settings', {'id': 'main', 'enabled': 'yes', 'mode': 'unknown'})
    rules = engine.publication_rules()
    assert rules['enabled'] is False and rules['mode'] == 'manual'
    engine.publish_tick()


def test_writing_rules_forbid_invented_citation_ids():
    from newsroom.llm import RULES, TASKS
    assert '不要自行发明' in RULES
    assert '标题不超过 32' in TASKS['write']
    assert 'format_instruction' in TASKS['write']


def _with_review(engine, draft, **review):
    from newsroom.models import content_hash
    draft['review'] = {'mode': 'advisory', 'passed': True, 'score': 88.0, 'issues': [], 'summary': '通过'}
    draft['review'].update(review)
    draft['review_hash'] = content_hash(draft['article'])
    engine.store.put('drafts', draft)
    return draft


def test_auto_approve_blocks_advisory_major_issues(tmp_path):
    from tests.test_publication import real_draft
    engine = make(tmp_path)
    draft = real_draft(engine)
    _with_review(engine, draft, score=74.0,
                 issues=[{'severity': 'major', 'category': '结构冗余', 'detail': '多节重复'}])
    engine.save_publish_config(PublishConfigPatch(auto_approve=True))
    assert engine.auto_approve(draft['id'], draft['revision']) is None
    assert engine.store.get('drafts', draft['id'])['status'] == 'pending_human'
    assert any(e['stage'] == 'auto_approve_blocked' for e in engine.store.events())


def test_auto_approve_blocks_low_score_even_without_issues(tmp_path):
    from tests.test_publication import real_draft
    engine = make(tmp_path)
    draft = real_draft(engine)
    _with_review(engine, draft, score=70.0)
    engine.save_publish_config(PublishConfigPatch(auto_approve=True))
    assert engine.auto_approve(draft['id'], draft['revision']) is None


def test_auto_approve_allows_clean_advisory_review(tmp_path):
    from tests.test_publication import real_draft
    engine = make(tmp_path)
    draft = real_draft(engine)
    _with_review(engine, draft, score=88.0)
    engine.save_publish_config(PublishConfigPatch(auto_approve=True))
    assert engine.auto_approve(draft['id'], draft['revision'])['status'] == 'ready_to_publish'
