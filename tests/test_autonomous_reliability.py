import copy
import json

import httpx
import pytest

from newsroom.collect import demo_items, matches_keywords
from newsroom.config import Settings, sources
from newsroom.engine import Engine
from newsroom.llm import ModelClient
from newsroom.model_contracts import clean_json_text
from newsroom.models import AutonomousPlan
from tests.test_crosscheck import fixtures, story
from tests.test_run_diagnostics import fake_completion


def make_engine(tmp_path, **kwargs):
    return Engine(Settings(data_dir=tmp_path, admin_token='test', **kwargs))


def test_complete_json_fence_only():
    assert clean_json_text('\ufeff ```json\n{"x":1}\n``` ') == '{"x":1}'
    assert clean_json_text('before ```json\n{}\n```').startswith('before')
    with pytest.raises(ValueError):
        clean_json_text(None)


def test_cache_reuses_valid_evidence_but_invalidates_changed_source(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, demo=False, llm_base_url='https://model.example/v1', llm_api_key='fake', llm_model='fake')
    news = demo_items()[0]
    good = ModelClient.demo('extract', {'news': news})
    calls = fake_completion(monkeypatch, [good])
    run = engine.new_run('test')
    for date in ('old', 'new'):
        engine.model.complete('extract', {'news': {**news, 'collected_at': date}}, run)
    assert len(calls) == 1
    assert engine.store.get('runs', run['id'])['calls'] == 1
    assert engine.store.all('calls')[0]['outcome'] == 'cache_hit'
    engine.model.complete('extract', {'news': {**news, 'text': news['text'] + ' changed'}}, run)
    assert len(calls) == 2


def test_crosscheck_pairs_are_small_and_reused(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, demo=False, llm_base_url='https://model.example/v1', llm_api_key='fake', llm_model='fake')
    a, b, evidence, first = fixtures()
    c = {**b, 'id': 'second'}
    second = copy.deepcopy(first); second['sources'][0]['story_id'] = c['id']
    calls = fake_completion(monkeypatch, [first, second])
    context = {'anchor': a, 'evidence': evidence, 'candidates': [b, c]}
    run = engine.new_run('test')
    output = engine.model.complete('crosscheck', context, run)
    assert [s['story_id'] for s in output['sources']] == [b['id'], c['id']]
    assert len(calls) == 2
    for call in calls:
        payload = json.loads(call['json']['messages'][1]['content'].split('\n', 1)[1])
        assert len(payload['candidates']) == 1
    engine.model.complete('crosscheck', context, run)
    assert len(calls) == 2


def test_autonomous_pipeline_writes_once_without_ratio_or_reviewer(tmp_path):
    engine = make_engine(tmp_path, editorial_mode='auto', review_mode='off', fast_mode=True)
    engine.enqueue('pipeline'); engine.work_once()
    draft = engine.store.all('drafts')[0]
    assert draft['status'] == 'pending_human'
    assert draft['review']['mode'] == 'off' and draft['review']['score'] is None
    stages = [c['stage'] for c in engine.store.all('calls')]
    assert stages.count('plan_article') == 1 and stages.count('write') == 1
    assert not set(stages) & {'prompt', 'review', 'angle', 'plan_auto', 'visual'}
    assert draft['team_contributions'] == [] and draft['editorial_plan']['mode'] == 'single_article'
    assert 'news_ratio_actual' not in draft['editorial_plan']
    assert draft['visuals']['state'] == 'automatic_layout'


def test_whole_article_plan_controls_length_without_extra_rewrite(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, editorial_mode='auto', review_mode='off')
    context = {'evidence': ModelClient.demo('extract', {'news': demo_items()[0]}), 'editor': {'id': 'auto'}}
    plan = ModelClient.demo('plan_article', context)
    plan['target_chars'] = 4200
    plan['narrative_beats'] = ['具体问题', '机制解释', '真实结果', '读者怎么用']
    stages = []
    def complete(stage, data, run, validator=None):
        stages.append(stage)
        if stage == 'plan_article': return copy.deepcopy(plan)
        assert data['writing_prompt'] == plan['writing_prompt'] and '4200' in data['length_instruction']
        assert 'section' not in data
        return {'title': '新消息', 'digest': '有出处', 'markdown': '字' * 1000 + '[F1]'}
    monkeypatch.setattr(engine.model, 'complete', complete)
    article, prompt, result, parts = engine.write_autonomously(context, {}, {'id': 'auto'}, engine.new_run('test'))
    assert stages == ['plan_article', 'write'] and not parts
    assert result['actual_chars'] == 1000 and result['length_status'] == 'short'
    assert result['narrative_beats'] == plan['narrative_beats']


def test_media_report_can_draft_in_relaxed_mode_but_conflicts_still_block(tmp_path):
    engine = make_engine(tmp_path, review_mode='off')
    a, b, evidence, output = fixtures()
    a = story('anchor', 'techcrunch-ai', a['title'], a['text'])
    output['sources'] = []
    assert engine.validate_crosscheck(a, evidence, [], output)['state'] == 'media_report'
    engine.settings.review_mode = 'strict'
    assert not engine.validate_crosscheck(a, evidence, [], output)['passed']
    engine.settings.review_mode = 'off'
    a, b, evidence, output = fixtures()
    output['sources'][0]['comparisons'][0]['judgment'] = 'contradicts'
    assert engine.validate_crosscheck(a, evidence, [b], output)['state'] == 'conflict'


@pytest.mark.parametrize('provenance_valid', [True, False])
def test_publisher_feed_fallback_requires_matching_provenance(tmp_path, monkeypatch, provenance_valid):
    engine = make_engine(tmp_path)
    a, _, _, _ = fixtures()
    source = engine.source_for(a)
    a.update(feed_html='<p>' + 'Registered publisher news content. ' * 20 + '</p>',
             feed_text='Registered publisher news content. ' * 20,
             feed_snapshot={'source_id': source['id'], 'feed_url': source['url'], 'article_url': a['url'] if provenance_valid else 'https://other.example/'})
    def failed(*args, **kwargs):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr('newsroom.collect.fetch_document', failed)
    if provenance_valid:
        result = engine.prepare_source(a)
        assert result['source_snapshot']['fetch_method'] == 'publisher_feed'
        assert result['source_snapshot']['page_date_verified'] is False
    else:
        with pytest.raises(httpx.ConnectError):
            engine.prepare_source(a)


def test_ai_keyword_filters_do_not_match_daily_or_paid():
    assert not matches_keywords('Daily paid news about Apple Pay', ['AI'])
    assert matches_keywords('New AI model', ['AI'])
    assert matches_keywords('国产大模型发布', ['大模型'])


def test_body_survives_unexpected_visual_failure(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, editorial_mode='auto', review_mode='off')
    def failed(*args):
        raise OSError('render unavailable')
    monkeypatch.setattr(engine, 'prepare_visuals', failed)
    engine.enqueue('pipeline'); engine.work_once()
    draft = engine.store.all('drafts')[0]
    assert draft['article']['markdown'] and draft['status'] == 'needs_ai_review'
    assert engine.store.get('stories', draft['story_id'])['status'] == 'drafted'
    assert engine.store.get('runs', draft['run_id'])['generated'] == 1


def test_added_ai_sources_have_explicit_identity_and_disabled_feeds_are_honest():
    catalog = {s['id']: s for s in sources()}
    for ident in ('the-decoder', 'marktechpost', 'leiphone', 'interconnects', 'latent-space', 'nvidia-blog', 'meta-engineering'):
        row = catalog[ident]
        assert row['enabled'] and row['allowed_hosts'] and row['organization']
    assert not catalog['venturebeat-ai']['enabled'] and not catalog['qbitai']['enabled']


def test_curated_direct_tun_ipv6_requires_independent_dns(monkeypatch):
    from newsroom.collect import public_url
    verified = []
    monkeypatch.setattr('newsroom.collect.socket.getaddrinfo', lambda *a, **kw: [
        (2, 1, 6, '', ('198.18.0.138', 443)), (23, 1, 6, '', ('2001:2::75', 443, 0, 0))])
    monkeypatch.setattr('newsroom.collect.verify_public_dns', lambda host, **kw: verified.append(host))
    assert public_url('https://www.minimax.cn/blog/test', curated=True, proxy_active=False)
    assert verified == ['www.minimax.cn']
    with pytest.raises(ValueError, match='内网'):
        public_url('https://unregistered.example/test', curated=True, proxy_active=False)


def test_advisory_review_network_failure_still_delivers_without_fake_score(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, review_mode='off')
    engine.enqueue('pipeline'); engine.work_once()
    draft = engine.store.all('drafts')[0]
    draft.update(status='needs_ai_review', review=None)
    engine.store.put('drafts', draft)
    engine.settings.review_mode = 'advisory'
    def failed(*a, **kw):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(engine.model, 'complete', failed)
    engine.perform_review(draft, engine.new_run('test'), {})
    current = engine.store.get('drafts', draft['id'])
    assert current['status'] == 'pending_human' and current['delivery']
    assert current['review']['score'] is None and current['review']['mode'] == 'advisory'


def test_direct_dns_uses_domestic_https_and_still_rejects_private(monkeypatch):
    from newsroom.collect import DNS_CACHE, verify_public_dns
    calls = []
    class FakeClient:
        def __init__(self, **kw):
            assert kw['trust_env'] is False
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url, **kw):
            calls.append(url)
            return httpx.Response(200, json={'Status': 0, 'Answer': [{'type': 1, 'data': '127.0.0.1'}]}, request=httpx.Request('GET', url))
    monkeypatch.setattr('newsroom.collect.httpx.Client', FakeClient)
    DNS_CACHE.pop('www.minimax.cn', None)
    with pytest.raises(ValueError, match='公网 DNS'):
        verify_public_dns('www.minimax.cn', proxy_active=False)
    assert calls == ['https://dns.alidns.com/resolve']


def test_news_direct_transport_failure_can_use_news_proxy(tmp_path, monkeypatch):
    from newsroom.collect import fetch_source
    settings = Settings(data_dir=tmp_path, news_proxy_url='http://127.0.0.1:7897')
    routes = []
    def fetch(url, **kw):
        routes.append(kw['settings'].news_proxy_url)
        if len(routes) == 1:
            raise httpx.ConnectError('TLS interrupted')
        return {'html': 'publisher page', 'url': url}
    monkeypatch.setattr('newsroom.collect.fetch_document', fetch)
    result = fetch_source({'network': 'direct', 'allowed_hosts': ['www.minimax.cn']}, 'https://www.minimax.cn/news', settings, document=True)
    assert routes == ['', settings.news_proxy_url] and result['route_fallback'] == 'news_proxy'
    assert settings.news_proxy_url == 'http://127.0.0.1:7897'


@pytest.mark.parametrize('allow_cached', [True, False])
def test_only_local_opt_in_can_reuse_original_during_network_failure(tmp_path, monkeypatch, allow_cached):
    import hashlib
    from tests.test_source_stability import fixture
    engine = make_engine(tmp_path)
    source, draft = fixture(engine)
    draft['source_snapshot']['body_hash'] = hashlib.sha256(source['text'].encode()).hexdigest()
    old_checked = draft['source_snapshot']['checked_at']
    def failed(*a, **kw): raise httpx.ConnectError('offline')
    monkeypatch.setattr(engine, 'prepare_source', failed)
    if allow_cached:
        result = engine.verify_source(draft, force=True, allow_cached=True)
        assert result['source_snapshot']['verification_method'] == 'cached_original'
        assert result['source_snapshot']['checked_at'] == old_checked
    else:
        with pytest.raises(ValueError, match='信源复查'):
            engine.verify_source(draft, force=True)


@pytest.mark.parametrize('has_known_conflict', [True, False])
def test_relaxed_partial_crosscheck_keeps_conflicts_but_does_not_lose_source_only_draft(tmp_path, monkeypatch, has_known_conflict):
    from newsroom.crosscheck import CorroborationPending
    engine = make_engine(tmp_path, review_mode='off')
    a, b, evidence, output = fixtures()
    c = {**b, 'id': 'unavailable'}
    monkeypatch.setattr(engine, 'related_stories', lambda anchor: [b, c])
    monkeypatch.setattr(engine, 'prepare_source', lambda item, **kw: item)
    def complete(stage, context, run, validator=None):
        if context['candidates'][0]['id'] == 'unavailable' or not has_known_conflict:
            raise ValueError('model network or schema failure')
        result = copy.deepcopy(output)
        result['sources'][0]['comparisons'][0]['judgment'] = 'contradicts'
        validator(result)
        return result
    monkeypatch.setattr(engine.model, 'complete', complete)
    if has_known_conflict:
        with pytest.raises(CorroborationPending):
            engine.crosscheck_story(a, evidence, engine.new_run('test'))
        assert engine.store.get('stories', a['id'])['crosscheck']['state'] == 'conflict'
    else:
        report = engine.crosscheck_story(a, evidence, engine.new_run('test'))
        assert report['state'] == 'primary_announcement' and report['passed']
        assert len(report['failures']) == 2 and not report['sources']


def test_real_feed_collection_uses_bounded_parallel_io(tmp_path, monkeypatch):
    import threading
    from datetime import datetime, timezone
    from newsroom.collect import collect
    barrier = threading.Barrier(2)
    threads = set()
    date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    def fetch(url, **kw):
        threads.add(threading.get_ident())
        barrier.wait(timeout=10)
        return '<rss><channel><title>Test</title><item><title>AI release</title><link>' + url + '/post</link><pubDate>' + date + '</pubDate></item></channel></rss>'
    monkeypatch.setattr('newsroom.collect.fetch_text', fetch)
    catalog = [{'id': str(i), 'name': str(i), 'url': 'https://example.org/' + str(i)} for i in range(2)]
    result = collect(catalog, 72, lambda *a: None, settings=Settings(data_dir=tmp_path, demo=False))
    assert len(result) == 2 and len(threads) == 2
