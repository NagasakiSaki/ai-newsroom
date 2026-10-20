import io
import json
import socket
from zipfile import ZipFile

import httpx
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings, sources
from newsroom.engine import Engine
from newsroom.editorial_team import image_candidates, asset_file, original_asset_file
from newsroom.images import image_url, fetch_image, save_source_image, SourceImageError
from newsroom.render import render_article
from tests.test_publication import approved, enable, FakeWechat


def picture(fmt='PNG', noisy=False):
    image = Image.effect_noise((2000, 1200), 100).convert('RGB') if noisy else Image.new('RGB', (640, 400), '#416bb8')
    out = io.BytesIO(); image.save(out, format=fmt)
    return out.getvalue()


def candidate(url='https://cdn.example/official.png'):
    return {'id': 'I1', 'url': url, 'source_url': 'https://vendor.example/news/release',
            'source_name': '厂商官方', 'origin': 'official', 'caption': '产品原始示意图'}


def real_source_fixture(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path, admin_token='test', wechat_app_id='wxFAKE123', wechat_app_secret='fake'))
    engine.wechat = engine.fake = FakeWechat()
    draft = approved(engine)
    source = next(s for s in sources() if s['id'] == 'google-research')
    url = 'https://research.google/blog/test-news/'
    story = engine.store.get('stories', draft['story_id'])
    story.update(source_id=source['id'], source=source['name'], url=url,
        source_snapshot={'body_hash': 'source-hash'},
        image_candidates=image_candidates('<article><figure><img src="https://cdn.example/chart.png" alt="官方实验图"><figcaption>发布中的原始图表</figcaption></figure></article>', url, source))
    engine.store.put('stories', story)
    draft.update(news_url=url, source=source['name'])
    for fact in draft['evidence']['facts']:
        fact['source_url'] = url
    return engine, draft


def test_candidates_capture_official_original_and_high_resolution_srcset():
    images = image_candidates('<article><img src="thumb.png" srcset="small.png 320w, large.png 1600w" alt="模型架构"><img src="/logo.svg"><img src="javascript:evil()"></article>',
        'https://vendor.example/news/release', {'id': 'vendor', 'name': '厂商', 'kind': 'official'})
    assert len(images) == 1 and images[0]['url'] == 'https://vendor.example/news/large.png'
    assert images[0]['usage'] == 'source_original' and images[0]['source_name'] == '厂商'


def test_body_images_prefer_real_lightbox_originals_and_exclude_related_posts():
    html = '<meta property="og:image" content="/generic.jpg"><main><img src="/images/chart.width-1250.png" alt="实验图表"><div class="related-posts"><img src="/unrelated.jpg"></div><div class="image-modal"><img src="/original_images/chart.png"></div></main>'
    images = image_candidates(html, 'https://vendor.example/news', {'kind': 'official'})
    assert len(images) == 1
    assert images[0]['url'] == 'https://vendor.example/original_images/chart.png'
    assert images[0]['caption'] == '实验图表'


@pytest.mark.parametrize('url', ['https://127.0.0.1/image.png', 'https://192.168.1.1/image.png',
    'https://localhost/image.png', 'https://[::1]/image.png', 'https://example.com:8443/image.png',
    'http://example.com/image.png'])
def test_official_image_download_rejects_internal_and_unsafe_urls(url):
    with pytest.raises(ValueError):
        image_url(url)


def test_cdn_fake_ip_requires_independent_public_dns(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET,socket.SOCK_STREAM,6,'',('198.18.0.25',443))])
    checks = []
    monkeypatch.setattr('newsroom.images.verify_public_dns', lambda host, **kw: checks.append((host,kw['proxy_active'])))
    assert image_url('https://cdn.example/a.png',proxy_active=True).startswith('https://cdn.example/')
    assert checks == [('cdn.example', True)]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.2',443))])
    with pytest.raises(ValueError): image_url('https://cdn.example/a.png')


def test_image_download_preserves_signed_query_order_and_path(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    url = 'https://cdn.example/asset/?z=2&a=1&sig=abc%2Fdef'
    assert image_url(url + '#preview') == url


def test_media_anchor_can_use_same_event_official_images_but_not_weak_matches(tmp_path):
    engine, draft = real_source_fixture(tmp_path)
    original_story = engine.store.get('stories', draft['story_id'])
    official_id = original_story['id'] + '-official'
    engine.store.put('stories', {**original_story, 'id': official_id})
    media = next(s for s in sources() if s['id'] == 'techcrunch-ai')
    original_story.update(source_id=media['id'], source=media['name'], url='https://techcrunch.com/test-release',image_candidates=[])
    engine.store.put('stories', original_story)
    draft['crosscheck'] = {'sources': [{'story_id': official_id, 'same_event': True, 'confidence': .95}]}
    assert engine.collect_image_candidates(draft)[0]['usage'] == 'source_original'
    draft['crosscheck']['sources'][0]['confidence'] = .1
    assert engine.collect_image_candidates(draft) == []


def test_redirect_cannot_forward_image_request_to_localhost_and_has_no_credentials(monkeypatch):
    seen=[]
    original_client=httpx.Client
    def handle(request):
        seen.append(request)
        return httpx.Response(302,headers={'location':'https://127.0.0.1/private'},request=request)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    monkeypatch.setattr('newsroom.images.httpx.Client',lambda **kw: original_client(transport=httpx.MockTransport(handle),**kw))
    with pytest.raises(ValueError): fetch_image('https://cdn.example/image.png','https://vendor.example/article')
    assert len(seen)==1 and 'authorization' not in seen[0].headers
    assert seen[0].headers['referer']=='https://vendor.example/article'


def test_download_preserves_original_png_bytes_and_provenance(tmp_path):
    raw=picture(); info=save_source_image(tmp_path,candidate(),raw,candidate()['url'])
    assert original_asset_file(tmp_path,info).read_bytes()==raw
    assert asset_file(tmp_path,info).read_bytes()==raw
    assert info['kind']=='official_image' and not info['normalized_for_wechat']
    assert '官方原图' in info['credit'] and '原创信息图' not in info['credit']


def test_large_original_is_preserved_while_wechat_copy_is_compressed_without_crop(tmp_path):
    raw=picture(noisy=True); assert len(raw)>1_000_000
    info=save_source_image(tmp_path,candidate(),raw,candidate()['url'])
    assert original_asset_file(tmp_path,info).read_bytes()==raw
    assert info['normalized_for_wechat'] and len(asset_file(tmp_path,info).read_bytes())<1_000_000
    assert abs(info['width']/info['height'] - 2000/1200)<.01
    assert info['original_width']==2000 and info['original_height']==1200


def test_webp_keeps_full_original_and_creates_jpeg_display_copy(tmp_path):
    raw=picture('WEBP');info=save_source_image(tmp_path,candidate(),raw,candidate()['url'])
    assert info['original_file'].endswith('.webp') and info['file'].endswith('.jpg')
    assert original_asset_file(tmp_path,info).read_bytes()==raw


@pytest.mark.parametrize('raw', [b'<html>login page</html>', b'not an image'])
def test_invalid_source_images_never_become_public_assets(tmp_path,raw):
    with pytest.raises(SourceImageError):save_source_image(tmp_path,candidate(),raw,candidate()['url'])
    assert not (tmp_path/'media').exists()


def test_official_images_flow_into_preview_zip_and_wechat_draft(tmp_path,monkeypatch):
    engine,draft=real_source_fixture(tmp_path)
    downloads=[]
    def fetch(url,source_url,settings):
        downloads.append(url);return picture(),url
    monkeypatch.setattr('newsroom.editorial_team.fetch_image',fetch)
    engine.prepare_visuals(draft,engine.new_run('visual-test'))
    originals=[a for a in draft['visuals']['assets'] if a.get('kind')=='official_image']
    assert len(originals)==1 and draft['visuals']['source_images']['selected_count']==1
    engine.prepare_visuals(draft,engine.new_run('visual-test'))
    assert len(downloads)==1
    asset=originals[0]
    html=render_article(draft)
    assert asset['original_url'] in html and '官方原图' in html
    draft.update(status='pending_human',approved_hash=None);engine.store.put('drafts',draft)
    engine.approve(draft['id'],draft['revision'],'编辑',True)
    archive=tmp_path/'exports'/draft['id']/('v'+str(draft['revision']))/'handoff.zip'
    with ZipFile(archive) as package:
        assert 'originals/'+asset['original_file'] in package.namelist()
        assert package.read('originals/'+asset['original_file'])==picture()
        assert ('originals/'+asset['original_file']).encode() in package.read('article.html')
    enable(engine,mode='draft');pub=engine.create_publication(draft['id'],draft['revision']);engine.work_once()
    assert engine.get_publication(pub['id'])['status']=='draft_only'
    assert len([c for c in engine.fake.calls if c[0]=='media/uploadimg'])==2
    payload=next(c[1] for c in engine.fake.calls if c[0]=='draft/add')
    assert '官方原图' in payload['content'] and '/media/' not in payload['content']
    assert not any(c[0]=='freepublish/submit' for c in engine.fake.calls)


def test_original_download_failure_is_reported_and_not_mislabeled_as_success(tmp_path,monkeypatch):
    engine,draft=real_source_fixture(tmp_path)
    def fail(*a,**kw):raise SourceImageError('原图下载返回 HTTP 403')
    monkeypatch.setattr('newsroom.editorial_team.fetch_image',fail)
    engine.prepare_visuals(draft,engine.new_run('visual-test'))
    assert draft['visuals']['source_images']['selected_count']==0
    assert draft['visuals']['source_images']['errors'][0]['error'].endswith('403')
    assert all(a['kind']=='infographic' for a in draft['visuals']['assets'])


def test_legacy_wechat_draft_delivery_also_uploads_official_originals(tmp_path, monkeypatch):
    from newsroom.wechat import Wechat
    engine, draft = real_source_fixture(tmp_path)
    monkeypatch.setattr('newsroom.editorial_team.fetch_image', lambda url, *a: (picture(), url))
    engine.prepare_visuals(draft, engine.new_run('visual-test'))
    client = Wechat(engine.settings)
    uploads, payloads = [], []
    monkeypatch.setattr(client, 'cover_id', lambda: 'fake-cover')
    def upload(path):
        uploads.append(path)
        return 'https://mmbiz.qpic.cn/' + path.name
    monkeypatch.setattr(client, 'upload_inline_image', upload)
    monkeypatch.setattr(client, 'add_draft', lambda payload: payloads.append(payload) or 'fake-draft')
    assert client.create_draft(draft) == 'fake-draft'
    assert len(uploads) == 2 and '/media/' not in payloads[0]['content']
    assert '官方原图' in payloads[0]['content']


def test_original_hash_change_blocks_reuse(tmp_path):
    asset=save_source_image(tmp_path,candidate(),picture(),candidate()['url'])
    original_asset_file(tmp_path,asset).write_bytes(b'tampered')
    with pytest.raises(ValueError,match='变化'): original_asset_file(tmp_path,asset)


def test_visual_service_test_route_accepts_its_own_response_profile(tmp_path,monkeypatch):
    app=create_app(Settings(data_dir=tmp_path,admin_token='test',llm_base_url='https://model.example',
        llm_api_key='fake',llm_model='writer'),start_worker=False)
    def post(self,url,**kw):return httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]},request=httpx.Request('POST',url))
    with TestClient(app) as client:
        with monkeypatch.context() as patch:
            patch.setattr(httpx.Client,'post',post)
            result=client.request('POST','/api/config/models/visual/test',headers={'Authorization':'Bearer test'})
        assert result.status_code==200 and result.json()['profile']=='visual' and result.json()['ok']
