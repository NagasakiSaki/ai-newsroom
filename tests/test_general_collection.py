import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.crawler import Session, crawl_source
from newsroom.source_catalog import CollectionSettings, CrawlSource, matches, save


TEXT = '这是一份关于乡村教育和公共图书馆建设的公开资料，介绍课程安排、阅读空间和社区志愿者的合作方式。' * 8
DATE = datetime.now(timezone.utc).isoformat()


def article(title='乡村教育的新实践', body=TEXT, date=None):
    meta = '<meta property="article:published_time" content="' + date + '">' if date else ''
    return '<html><head>' + meta + '</head><body><article><h1>' + title + '</h1><p>' + body + '</p></article></body></html>'


@pytest.fixture
def site(monkeypatch):
    pages, seen = {}, []
    def fetch(url, **kwargs):
        seen.append(url)
        value = pages.get(url, 'User-agent: *\nAllow: /' if url.endswith('/robots.txt') else None)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ValueError('test page missing')
        return value if isinstance(value, dict) else {'url': url, 'html': value}
    monkeypatch.setattr('newsroom.collect.fetch_document', fetch)
    monkeypatch.setattr(Session, 'pause', lambda self, delay: None)
    return pages, seen


def source(**kwargs):
    return CrawlSource(name='教育资料', url='https://publisher.example/news/', **kwargs).registered()


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'http://127.0.0.1/x', 'https://169.254.169.254/latest/',
    'http://192.168.1.3/', 'https://[::1]/', 'https://localhost/', 'https://host.local/',
    'https://user:password@publisher.example/', 'https://publisher.example:8080/',
    'https://publisher.example/?api_key=private', 'https://publisher.example/?token=private',
])
def test_source_url_rejects_private_or_credential_urls(url):
    with pytest.raises(ValidationError):
        CrawlSource(name='unsafe', url=url)


def test_source_models_validate_budget_selector_and_keyword_rules():
    with pytest.raises(ValidationError):
        source(max_pages=31)
    with pytest.raises(ValidationError):
        source(content_selector='[')
    with pytest.raises(ValidationError):
        CollectionSettings(custom_sources=[CrawlSource(id='custom-one', name='a', url='https://a.example/'),
                                           CrawlSource(id='custom-one', name='b', url='https://b.example/')])
    config = CollectionSettings(keywords=['教育', '图书馆'], keyword_mode='all', exclude_keywords=['广告'])
    assert matches(TEXT, config)
    assert not matches('只有教育内容', config)
    assert not matches(TEXT + '广告', config)
    assert not matches('BRAIN', CollectionSettings(keywords=['AI']))
    assert matches('AI research', CollectionSettings(keywords=['AI']))


def test_html_crawler_keeps_unknown_date_and_full_text_with_no_models(site):
    pages, seen = site
    pages['https://publisher.example/news/'] = '<main><h2><a href="/one">乡村教育新实践</a></h2><h2><a href="https://other.example/private">外部资料链接</a></h2></main>'
    pages['https://publisher.example/one'] = article()
    rows, report = crawl_source(source(), CollectionSettings(keywords=['图书馆']), Settings(data_dir=None))
    assert len(rows) == 1 and TEXT in rows[0]['text']
    assert rows[0]['published_at'] is None and rows[0]['date_basis'] == 'unknown'
    assert rows[0]['status'] == 'discovery_only' and not rows[0]['source_report']['auto_eligible']
    assert report['model_calls'] == 0 and report['pages_fetched'] == 2
    assert not any('other.example' in url for url in seen)


def test_robots_denial_stops_request_and_reports_failure(site):
    pages, seen = site
    pages['https://publisher.example/robots.txt'] = 'User-agent: *\nDisallow: /news/'
    rows, report = crawl_source(source(), CollectionSettings(), Settings(data_dir=None))
    assert not rows and report['status'] == 'error'
    assert seen == ['https://publisher.example/robots.txt']
    assert 'robots.txt' in report['errors'][0]['error']


def test_robots_unreachable_is_not_silently_allowed(site):
    pages, seen = site
    pages['https://publisher.example/robots.txt'] = httpx.ConnectError('network unavailable')
    rows, report = crawl_source(source(), CollectionSettings(), Settings(data_dir=None))
    assert not rows and report['status'] == 'error'
    assert seen == ['https://publisher.example/robots.txt']


def test_rss_reads_article_and_uses_publisher_date(site):
    pages, _ = site
    pages['https://publisher.example/news/'] = '<rss version="2.0"><channel><title>教育</title><item><title>教育案例</title><link>https://publisher.example/one</link><pubDate>Tue, 20 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>'
    pages['https://publisher.example/one'] = article()
    rows, report = crawl_source(source(mode='rss'), CollectionSettings(), Settings(data_dir=None))
    assert len(rows) == 1 and rows[0]['published_at'] == '2026-10-20T08:00:00+00:00'
    assert rows[0]['date_basis'] == 'published'


def test_sitemap_and_page_budget(site):
    pages, seen = site
    pages['https://publisher.example/news/'] = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join('<url><loc>https://publisher.example/p%d</loc></url>' % i for i in range(10)) + '</urlset>'
    for i in range(10):
        pages['https://publisher.example/p%d' % i] = article(title='教育资料' + str(i))
    rows, report = crawl_source(source(mode='sitemap', max_pages=3), CollectionSettings(), Settings(data_dir=None))
    assert len(rows) == 2 and report['pages_fetched'] == 3 and report['truncated']
    assert len(seen) == 4  # Three content requests plus one robots.txt.


def test_selectors_full_body_keyword_and_age_filter(site):
    pages, _ = site
    pages['https://publisher.example/news/'] = '<div class="records"><a href="/one">专用列表资料</a></div>'
    pages['https://publisher.example/one'] = '<h1>新课程</h1><div class="body">' + TEXT + '</div>'
    rows, _ = crawl_source(source(article_selector='.records a', content_selector='.body'),
                           CollectionSettings(keywords=['图书馆']), Settings(data_dir=None))
    assert len(rows) == 1
    pages['https://publisher.example/one'] = article(date=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat())
    rows, report = crawl_source(source(), CollectionSettings(max_age_hours=24), Settings(data_dir=None))
    assert not rows and report['filtered_count'] == 1


def test_partial_failure_keeps_successful_pages(site):
    pages, _ = site
    pages['https://publisher.example/news/'] = '<main><a href="/one">正常教育资料</a><a href="/two">失败教育资料</a></main>'
    pages['https://publisher.example/one'] = article()
    rows, report = crawl_source(source(), CollectionSettings(), Settings(data_dir=None))
    assert len(rows) == 1 and report['status'] == 'partial'


def test_crawler_404_robots_is_allowed_but_403_is_not(site):
    pages, _ = site
    url = 'https://publisher.example/robots.txt'
    pages['https://publisher.example/news/'] = article()
    def error(code):
        return httpx.HTTPStatusError('private response text', request=httpx.Request('GET', url), response=httpx.Response(code))
    pages[url] = error(404)
    rows, _ = crawl_source(source(mode='article'), CollectionSettings(), Settings(data_dir=None))
    assert len(rows) == 1
    pages[url] = error(403)
    rows, report = crawl_source(source(mode='article'), CollectionSettings(), Settings(data_dir=None))
    assert not rows and 'private response text' not in str(report)


def test_collection_api_persistence_no_key_needed_and_library_exports(tmp_path, site, monkeypatch):
    pages, _ = site
    pages['https://publisher.example/news/'] = article(title='=教育资料', date=None)
    settings = Settings(data_dir=tmp_path, admin_token='test', demo=False, scheduler=False)
    app = create_app(settings, start_worker=False)
    engine = app.state.engine
    monkeypatch.setattr(engine.model, 'complete', lambda *a, **k: pytest.fail('collection must not call model'))
    headers = {'Authorization': 'Bearer test'}
    with TestClient(app) as client:
        assert client.get('/api/sources/settings').status_code == 401
        config = client.get('/api/sources/settings', headers=headers).json()
        config.update(include_builtins=False, topic='教育', keywords=['图书馆'], custom_sources=[{
            'name': '公开教育资料', 'url': 'https://publisher.example/news/', 'mode': 'article'}])
        saved = client.put('/api/sources/settings', headers=headers, json=config)
        assert saved.status_code == 200 and saved.json()['revision'] == 1
        assert client.put('/api/sources/settings', headers=headers, json=config).status_code == 409
        assert not engine.store.all('jobs') and not engine.store.all('calls')
        assert client.post('/api/sources/check', headers=headers).status_code == 200
        engine.work_once()
        assert not engine.store.all('calls') and not engine.store.all('image_calls')
        rows = client.get('/api/library?q=图书馆', headers=headers).json()
        assert rows['total'] == 1 and rows['items'][0]['published_at'] is None
        ident = rows['items'][0]['id']
        assert TEXT in client.get('/api/library/' + ident, headers=headers).json()['text']
        assert 'crawl_html' not in client.get('/api/library/' + ident, headers=headers).json()
        assert client.get('/api/library?limit=201', headers=headers).status_code == 422
        assert client.get('/api/library/export?format=json', headers=headers).json()['total'] == 1
        assert "'=教育资料" in client.get('/api/library/export?format=csv', headers=headers).text
        state = client.get('/api/state', headers=headers).json()
        assert state['collection_settings']['topic'] == state['editorial']['topic'] == '教育'
        assert len(state['sources']) == 1 and 'crawl_html' not in state['stories'][0]
        # Missing publication time is retained for reference, but not smuggled into an automatic news run.
        client.post('/api/run', headers=headers, json={'article_count': 1})
        # Key validation would stop writing before paid calls; collection above remains useful.
        assert not engine.store.all('drafts')
    again = create_app(settings, start_worker=False)
    assert again.state.engine.collection_settings().topic == '教育'
    assert len(again.state.engine.store.all('stories')) == 1


def test_source_settings_reject_change_during_jobs_and_hide_bad_url_input(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, admin_token='test'), start_worker=False)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer test'}
        result = client.put('/api/sources/settings', headers=headers, json={'custom_sources':[
            {'name':'bad', 'url':'https://example.org/?token=DO-NOT-ECHO'}]})
        assert result.status_code == 422 and 'DO-NOT-ECHO' not in result.text
        app.state.engine.enqueue('collect')
        assert client.put('/api/sources/settings', headers=headers, json={}).status_code == 409


def test_collection_fingerprint_invalidates_recent_collection(tmp_path):
    from newsroom.engine import Engine
    engine = Engine(Settings(data_dir=tmp_path))
    from newsroom.db import now
    engine.store.put('runs', {'id':'collection', 'kind':'collection', 'status':'done', 'finished_at':now()})
    assert engine.recent_collection()
    save(engine.store, CollectionSettings(topic='历史'))
    assert engine.recent_collection() is None


def test_extra_hosts_are_explicit_and_not_credential_or_private_addresses():
    assert source(extra_hosts=['www.publisher.example'])['allowed_hosts'] == ['publisher.example', 'www.publisher.example']
    for host in ('127.0.0.1', '*.example.org', 'https://example.org', 'user:password@example.org', 'local.internal'):
        with pytest.raises(ValidationError):
            source(extra_hosts=[host])


def test_feed_can_follow_explicit_article_domain_and_checks_its_robots(site):
    pages, seen = site
    pages['https://publisher.example/news/'] = '<rss version="2.0"><channel><item><link>https://articles.example/one</link></item></channel></rss>'
    pages['https://articles.example/one'] = article()
    rows, _ = crawl_source(source(mode='rss', extra_hosts=['articles.example']), CollectionSettings(), Settings(data_dir=None))
    assert len(rows) == 1 and 'https://articles.example/robots.txt' in seen


def test_redirect_is_checked_against_robots_before_target_request(monkeypatch):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nDisallow: /private')
        if request.url.path == '/news/':
            return httpx.Response(302, headers={'location':'/private'})
        pytest.fail('disallowed redirect must not be requested')
    real_client = httpx.Client
    monkeypatch.setattr('newsroom.collect.httpx.Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr('newsroom.collect.socket.getaddrinfo', lambda *a, **k:[(None,None,None,None,('8.8.8.8',443))])
    monkeypatch.setattr(Session, 'pause', lambda *a: None)
    rows, report = crawl_source(source(mode='article'), CollectionSettings(), Settings(data_dir=None))
    assert not rows and report['status'] == 'error'
    assert seen == ['https://publisher.example/robots.txt', 'https://publisher.example/news/']


def test_custom_hosts_with_tun_require_independent_public_dns(monkeypatch):
    from newsroom.collect import public_url
    verified = []
    monkeypatch.setattr('newsroom.collect.verify_public_dns', lambda host, **kw: verified.append(host))
    assert public_url('https://publisher.example/news', curated=True, proxy_active=True,
                      trusted_hosts=['publisher.example']).startswith('https://publisher.example/')
    assert verified == ['publisher.example']
    monkeypatch.setattr('newsroom.collect.socket.getaddrinfo', lambda *a, **k:[(None,None,None,None,('127.0.0.1',443))])
    with pytest.raises(ValueError):
        public_url('https://publisher.example/news', curated=True, proxy_active=False, trusted_hosts=['publisher.example'])


def test_cancellation_preserves_already_delivered_items(site):
    from newsroom.workflow_control import WorkflowCancelled
    pages, seen = site
    pages['https://publisher.example/news/'] = '<main><a href="/one">教育资料之一</a><a href="/two">教育资料之二</a></main>'
    pages['https://publisher.example/one'] = article()
    saved = []
    def checkpoint():
        if saved:
            raise WorkflowCancelled()
    with pytest.raises(WorkflowCancelled):
        crawl_source(source(), CollectionSettings(), Settings(data_dir=None), checkpoint, saved.append)
    assert len(saved) == 1 and 'https://publisher.example/two' not in seen


def test_unrelated_time_tags_do_not_become_document_publication_dates():
    from newsroom.crawler import extract
    html = '<main><h1>教育资料</h1><p>' + TEXT + '</p><aside><time datetime="' + DATE + '">活动日期</time></aside></main>'
    parsed = extract(html, 'https://publisher.example/one', source())
    assert not parsed.get('published_at')
    own = '<article><header><h1>教育资料</h1><time datetime="' + DATE + '">发布日期</time></header><p>' + TEXT + '</p></article>'
    assert extract(own, 'https://publisher.example/one', source())['published_at'] == DATE


def test_redirect_guard_applies_configured_crawl_delay(monkeypatch):
    from urllib.robotparser import RobotFileParser
    parser = RobotFileParser()
    parser.parse(['User-agent: *', 'Allow: /', 'Crawl-delay: 3'])
    session = Session(source(), Settings(data_dir=None), None)
    session.robots['https://publisher.example'] = parser
    session.last_request['https://publisher.example'] = 100
    pauses = []
    monkeypatch.setattr('newsroom.crawler.time.monotonic', lambda: 100)
    monkeypatch.setattr(session, 'pause', pauses.append)
    session.guard_redirect('https://publisher.example/next')
    assert pauses == [3]


def test_keyword_search_urls_are_not_mistaken_for_api_keys():
    value = 'https://publisher.example/search?keyword=education&monkey=example'
    assert CrawlSource(name='Search results', url=value).url == value
    with pytest.raises(ValidationError):
        CrawlSource(name='unsafe', url='https://publisher.example/search?api-key=DO-NOT-SAVE')
