import copy
import io
import socket

import httpx
from PIL import Image
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.engine import Engine
from newsroom.editorial_team import image_candidates
from newsroom.images import fetch_image
from newsroom.llm import ModelClient
from newsroom.models import content_hash
from newsroom.visual_discovery import discover_story_images, official_link
from test_draft_workflow import configured


def png():
    stream = io.BytesIO()
    Image.new('RGB', (960, 540), '#294785').save(stream, format='PNG')
    return stream.getvalue()


def test_extracts_domestic_article_rss_fragments_and_hero_without_author_or_svg():
    html = '<meta property="og:image" content="https://cdn.example/hero.jpg"><div id="paragraph">' \
           '<div class="author"><img src="/person.jpg"></div><img data-src="/chart.png" src="/small.png">' \
           '<img src="/google-g.svg"></div>'
    rows = image_candidates(html, 'https://publisher.example/story')
    assert {r['url'] for r in rows} == {'https://publisher.example/chart.png'}
    assert rows[0]['alternative_urls'] == ['https://publisher.example/small.png']
    assert image_candidates('<article><div class="author"><img src="/person.jpg"></div></article>'
        '<meta property="og:image" content="/hero.jpg">', 'https://publisher.example/story')[0]['url'].endswith('/hero.jpg')
    assert len(image_candidates('<p><img src="https://cdn.example/rss.jpg"></p>', 'https://publisher.example/story')) == 1


def test_image_timeout_or_http_failure_can_fall_back_without_leaking_credentials(monkeypatch):
    requests, routes = [], []
    original = httpx.Client
    def respond(request):
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 200, content=b'image', headers={'content-type':'image/png'})
    def client(**kwargs):
        routes.append(kwargs.pop('proxy', None))
        return original(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr('newsroom.images.httpx.Client', client)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2,1,6,'',('8.8.8.8',443))])
    data, _ = fetch_image('https://cdn.example/news.png', 'https://publisher.example/story',
                          Settings(data_dir=None, news_proxy_url='http://127.0.0.1:7897'))
    assert data == b'image' and routes == ['http://127.0.0.1:7897', None]
    assert len(requests) == 2 and all('authorization' not in r.headers for r in requests)


def test_actual_srcset_alternative_recovers_bad_file_and_is_cached(tmp_path, monkeypatch):
    engine, draft = configured(tmp_path)
    candidate = {'id':'I1','usage':'source_original','origin':'official','caption':'产品界面',
        'url':'https://cdn.example/bad.png','alternative_urls':['https://cdn.example/actual.png'],
        'source_url':draft['news_url'],'source_name':'原始来源'}
    requests = []
    def fetch(url, *a):
        requests.append(url)
        return (b'not a picture' if url.endswith('bad.png') else png()), url
    monkeypatch.setattr('newsroom.editorial_team.fetch_image', fetch)
    for _ in range(2):
        assets, report = engine.download_source_images([candidate], {'images':[{'image_id':'I1'}]}, draft)
        assert len(assets) == 1 and report['selected_count'] == 1
    assert requests == ['https://cdn.example/bad.png','https://cdn.example/actual.png']


def test_no_originals_means_missing_images_not_text_cards(tmp_path):
    engine, draft = configured(tmp_path)
    draft['visuals']['assets'] = [{'kind':'infographic','id':'legacy-card'}]
    engine.complete_visuals(draft, allow_generation=False)
    assert draft['visuals']['assets'] == []
    assert draft['visuals']['quality']['text_card_count'] == 0
    assert draft['visuals']['state'] == 'needs_images' and draft['visuals']['cover_asset_id'] is None


def test_old_cards_do_not_permanently_suppress_picture_recovery(tmp_path, monkeypatch):
    engine, draft = configured(tmp_path)
    draft['visuals'] = {'assets': [], 'article_hash': content_hash(draft['article'])}
    candidate = {'id':'I1','usage':'source_original','origin':'official','caption':'实际产品图',
        'url':'https://cdn.example/product.png','source_url':draft['news_url'],'source_name':'原始来源'}
    monkeypatch.setattr(engine, 'collect_image_candidates', lambda *a, **k: [candidate])
    monkeypatch.setattr('newsroom.editorial_team.fetch_image', lambda url, *a: (png(),url))
    engine.prepare_visuals(draft, None)
    assert len(draft['visuals']['assets']) == 1
    assert draft['visuals']['assets'][0]['kind'] == 'official_image'
    assert draft['visuals']['quality']['needs_more_images']


def test_short_feature_gets_one_additive_pass_and_honest_length_status(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path, demo=False, drafts_only=True, fast_mode=True))
    plan = ModelClient.demo('plan_article', {})
    plan.update(story_mode='breaking', target_chars=2800)
    evidence = ModelClient.demo('extract', {'news': {'url':'https://example.com/news'}})
    evidence['facts'] = [dict(evidence['facts'][0], id='F'+str(i)) for i in range(1,11)]
    base = {'title':'本次产品变化','digest':'本次新闻摘要','markdown':'导语内容。[F1]\n\n## 机制\n\n'+
            '一个具体机制的说明。'*90+'[F1]\n\n## 条件\n\n条件说明。[F1]\n\n## 读者影响\n\n原有结尾。[F1]'}
    extra = {**base, 'markdown':'## 新增使用流程\n\n'+'以明确假设来解释用户操作和实际条件。'*40+'[F1]'}
    stages = []
    def complete(stage, context, run):
        stages.append(stage)
        return copy.deepcopy(base if stage == 'write' else extra)
    engine.model.complete = complete
    article, _, result, _ = engine.write_autonomously({'evidence':evidence}, {}, {}, engine.new_run('test'), plan=plan)
    # 写完先补写，再做一次终稿润色；本例润色稿明显更短，会被拒绝并保留原稿。
    assert stages == ['write','expand_article','polish']
    assert result['target_chars'] == 4200 and result['length_status'] == 'short'
    assert result['expansion_status'] == 'added_once'
    assert article['markdown'].endswith('原有结尾。[F1]')


def test_primary_links_are_explicit_bounded_and_cached(tmp_path, monkeypatch):
    engine, draft = configured(tmp_path)
    story = engine.store.get('stories', draft['story_id'])
    story['feed_html'] = '<p><a href="https://blog.cloudflare.com/release/">原始公告</a><a href="https://evil.example/">ignore</a></p>'
    monkeypatch.setattr('newsroom.collect.fetch_source', lambda *a, **k: {'html':'<article>新闻原文</article>', 'url':story['url']})
    requests = []
    def fetch(url, **kwargs):
        requests.append(url)
        return {'html':'<article><img src="https://cdn.example/architecture.png"></article>', 'url':url}
    monkeypatch.setattr('newsroom.collect.fetch_document', fetch)
    result = discover_story_images(engine, story)
    assert result['linked_image_candidates'][0]['origin'] == 'official'
    assert result['linked_image_candidates'][0]['linked_from'] == story['url']
    discover_story_images(engine, result)
    assert requests == ['https://blog.cloudflare.com/release/']
    assert official_link('https://github.com/evil/deepseek-clone') is None
    assert official_link('https://blog.cloudflare.com.evil.example/release') is None


def test_image_retry_endpoint_only_enqueues_and_reuses_pending_job(tmp_path):
    engine, draft = configured(tmp_path)
    app = create_app(engine.settings, start_worker=False)
    with TestClient(app) as client:
        path = '/api/drafts/'+draft['id']+'/images/refresh'
        assert client.post(path,json={'revision':draft['revision']}).status_code == 401
        client.headers['Authorization'] = 'Bearer test'
        first = client.post(path,json={'revision':draft['revision']})
        assert first.status_code == 200 and first.json()['kind'] == 'images'
        assert client.post(path,json={'revision':draft['revision']}).json()['id'] == first.json()['id']


def test_image_retry_worker_preserves_body_and_disables_even_configured_generation(tmp_path, monkeypatch):
    engine, draft = configured(tmp_path)
    engine.settings.image_enabled = True
    observed = []
    def prepare(current, run, **kwargs):
        observed.append(kwargs)
        engine.complete_visuals(current, allow_generation=kwargs['allow_generation'])
    monkeypatch.setattr(engine, 'prepare_visuals', prepare)
    monkeypatch.setattr('newsroom.image_service.generate_image', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no paid generation')))
    job = engine.enqueue('images', draft['id'], draft['revision'])
    engine.work_once()
    current = engine.store.get('drafts', draft['id'])
    assert observed == [{'force_refresh': True, 'allow_generation': False}]
    assert current['article'] == draft['article'] and current['revision'] == draft['revision'] + 1
    assert engine.store.get('jobs',job['id'])['status'] == 'done'
