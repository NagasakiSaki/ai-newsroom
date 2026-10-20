import json
import os
from dataclasses import replace
from pathlib import Path
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.network import options, proxy_url
from newsroom.runtime_config import FILENAME, RuntimeConfigPatch, load_saved, persist


@pytest.fixture
def local(tmp_path):
    settings=Settings(data_dir=tmp_path, admin_token='fake-admin', wechat_app_id='wxFAKE123', wechat_app_secret='FAKE-SECRET-OLD')
    app=create_app(settings,start_worker=False)
    with TestClient(app,headers={'Authorization':'Bearer fake-admin'}) as client:
        yield client,app.state.engine


def real_patch():
    return {'demo':False,'models':{
        'writer':{'base_url':'https://writer.example/v1','api_key':'FAKE-WRITER-KEY','model':'writer-model'},
        'prompt':{'model':'prompt-model'},'review':{'model':'review-model'}},
        'news_proxy_url':'http://127.0.0.1:7897'}


def test_authenticated_read_and_save_never_send_network_or_echo_keys(local,monkeypatch):
    client,engine=local
    monkeypatch.setattr(httpx.Client,'post',lambda *a,**kw:pytest.fail('save/read must not call external services'))
    assert client.get('/api/config',headers={'Authorization':''}).status_code==401
    assert client.put('/api/config',headers={'Authorization':''},json={}).status_code==401
    before=client.get('/api/config').json()
    assert before['wechat']['secret_configured'] and 'FAKE-SECRET' not in json.dumps(before)
    response=client.put('/api/config',json=real_patch())
    assert response.status_code==200 and response.json()['ready']
    assert 'FAKE-WRITER-KEY' not in response.text and 'FAKE-SECRET-OLD' not in response.text
    assert engine.settings.demo is False and engine.model.settings is engine.settings
    assert engine.wechat.settings is engine.settings and engine.wechat_connection.settings is engine.settings
    assert not engine.store.all('jobs') and not engine.store.all('integrations')
    assert response.headers['cache-control']=='no-store'


def test_persistence_roundtrip_and_current_user_encryption(local,monkeypatch):
    client,engine=local
    assert client.put('/api/config',json=real_patch()).status_code==200
    restored=load_saved(engine.settings.data_dir)
    assert restored['llm_api_key']=='FAKE-WRITER-KEY' and restored['wechat_app_secret']=='FAKE-SECRET-OLD'
    disk=(engine.settings.data_dir/FILENAME).read_text(encoding='utf-8')
    if os.name=='nt':
        assert 'FAKE-WRITER-KEY' not in disk and 'FAKE-SECRET-OLD' not in disk
        assert 'windows-dpapi-v1' in disk
    else:
        assert (engine.settings.data_dir/FILENAME).stat().st_mode&0o777==0o600
    # Settings.load 自动应用页面配置，重启不回到 .env 默认值；不读取实际用户 .env。
    monkeypatch.setattr('newsroom.config.load_dotenv',lambda *a,**kw:None)
    monkeypatch.setenv('DATA_DIR',str(engine.settings.data_dir))
    monkeypatch.setenv('ADMIN_TOKEN','fake-admin')
    monkeypatch.setenv('DEMO_MODE','true')
    loaded=Settings.load()
    assert not loaded.demo and loaded.llm_api_key=='FAKE-WRITER-KEY'


def test_blank_preserves_explicit_clear_rotates_and_invalidates_cached_wechat(local,monkeypatch):
    client,engine=local
    engine.wechat.token='FAKE-CACHED-TOKEN';engine.wechat.expires=9999999999
    assert client.put('/api/config',json={'wechat':{'app_secret':''}}).status_code==200
    assert engine.settings.wechat_app_secret=='FAKE-SECRET-OLD' and not engine.wechat.token
    response=client.put('/api/config',json={'wechat':{'app_secret':'FAKE-SECRET-NEW'}})
    assert response.status_code==200 and engine.settings.wechat_app_secret=='FAKE-SECRET-NEW'
    response=client.put('/api/config',json={'wechat':{'clear_app_secret':True}})
    assert response.status_code==200 and not engine.settings.wechat_app_secret
    response=client.put('/api/config',json={'wechat':{'clear_app_secret':True,'app_secret':'FAKE-CONFLICT'}})
    assert response.status_code==409 and 'FAKE-CONFLICT' not in response.text


@pytest.mark.parametrize('body',[
    {'models':{'writer':{'base_url':'https://user:FAKE-PRIVATE@provider.example'}}},
    {'models':{'writer':{'base_url':'https://provider.example/v1?key=FAKE-PRIVATE'}}},
    {'models':{'writer':{'api_key':{'unexpected':'FAKE-PRIVATE'}}}},
    {'models':{'writer':{'model':'FAKE-PRIVATE\nheader'}}},
    {'wechat':{'app_secret':['FAKE-PRIVATE']}},
    {'unknown-secret':'FAKE-PRIVATE'},
    {'news_proxy_url':'http://user:FAKE-PRIVATE@127.0.0.1:7897'},
    {'news_proxy_url':'socks5://FAKE-PRIVATE:7897'},
])
def test_validation_errors_do_not_echo_sensitive_input(local,body):
    client,engine=local
    response=client.put('/api/config',json=body)
    assert response.status_code==422 and 'FAKE-PRIVATE' not in response.text
    assert not (engine.settings.data_dir/FILENAME).exists()


def test_incomplete_real_mode_and_failed_save_leave_previous_settings_untouched(local,monkeypatch):
    client,engine=local
    assert client.put('/api/config',json={'demo':False}).status_code==409
    assert engine.settings.demo and not (engine.settings.data_dir/FILENAME).exists()
    assert client.put('/api/config',json={}).status_code==200
    original=(engine.settings.data_dir/FILENAME).read_bytes()
    def fail(*args):raise OSError('FAKE-PRIVATE-error')
    monkeypatch.setattr('newsroom.runtime_config.os.replace',fail)
    response=client.put('/api/config',json={'wechat':{'app_secret':'FAKE-SECRET-NEW'}})
    assert response.status_code==409 and 'FAKE-PRIVATE' not in response.text
    assert engine.settings.wechat_app_secret=='FAKE-SECRET-OLD'
    assert (engine.settings.data_dir/FILENAME).read_bytes()==original
    assert not list(engine.settings.data_dir.glob('.settings-*.tmp'))


def test_running_job_or_wechat_probe_blocks_updates(local):
    client,engine=local
    job=engine.enqueue('collect');job['status']='running';engine.store.put('jobs',job)
    assert client.put('/api/config',json={}).status_code==409
    job['status']='done';engine.store.put('jobs',job)
    engine.wechat_connection.lock.acquire()
    try:assert client.put('/api/config',json={}).status_code==409
    finally:engine.wechat_connection.lock.release()
    assert client.put('/api/config',json={}).status_code==200


def test_changing_provider_requires_explicit_key_but_same_host_path_keeps_it(local):
    client,engine=local
    assert client.put('/api/config',json=real_patch()).status_code==200
    assert client.put('/api/config',json={'models':{'writer':{'base_url':'https://writer.example/v2'}}}).status_code==200
    assert engine.settings.llm_api_key=='FAKE-WRITER-KEY'
    response=client.put('/api/config',json={'models':{'writer':{'base_url':'https://other-provider.example/v1'}}})
    assert response.status_code==409 and engine.settings.llm_base_url=='https://writer.example/v2'
    assert client.put('/api/config',json={'models':{'writer':{'base_url':'https://other-provider.example/v1','api_key':'FAKE-NEW-KEY'}}}).status_code==200
    assert engine.settings.llm_api_key=='FAKE-NEW-KEY'


def test_provider_change_with_explicit_clear_can_be_saved_in_demo(local):
    client,engine=local
    assert client.put('/api/config',json=real_patch()).status_code==200
    assert client.put('/api/config',json={'demo':True,'models':{'writer':{'base_url':'https://other.example/v1','clear_api_key':True}}}).status_code==200
    assert not engine.settings.llm_api_key


def test_proxy_routes_are_separate_and_wechat_never_uses_proxy(local,monkeypatch):
    client,engine=local
    patch=real_patch();patch['models']['writer']['proxy_url']='http://127.0.0.1:7897'
    patch['models']['prompt']['proxy_url']='http://127.0.0.1:7898'
    assert client.put('/api/config',json=patch).status_code==200
    monkeypatch.setenv('ALL_PROXY','http://bad.example:1111')
    assert options(engine.settings,'news')['proxy']=='http://127.0.0.1:7897'
    assert options(engine.settings,'writer')['proxy']=='http://127.0.0.1:7897'
    assert options(engine.settings,'prompt')['proxy']=='http://127.0.0.1:7898'
    assert 'proxy' not in options(engine.settings,'review')
    assert options(engine.settings,'wechat')=={'trust_env':False,'follow_redirects':False}


def test_model_manual_test_uses_saved_route_once_and_redacts_provider_response(local,monkeypatch):
    client,engine=local
    assert client.put('/api/config',json=real_patch()).status_code==200
    calls=[]
    def post(self,url,**kw):
        calls.append((url,kw))
        return httpx.Response(200,json={'choices':[{'message':{'content':'FAKE-PRIVATE-provider-content'}}]})
    monkeypatch.setattr(httpx.Client,'post',post)
    response=client.request('POST','/api/config/models/prompt/test')
    assert response.json()['ok'] and 'FAKE-PRIVATE' not in response.text and len(calls)==1
    assert calls[0][0]=='https://writer.example/v1/chat/completions'
    assert calls[0][1]['headers']['Authorization']=='Bearer FAKE-WRITER-KEY'
    assert calls[0][1]['json']['model']=='prompt-model'
    assert not engine.store.all('calls') and not engine.store.all('jobs')


def test_explicit_news_proxy_verifies_curated_dns_without_local_dns(local,monkeypatch):
    from newsroom.collect import public_url
    client,engine=local
    engine.settings.news_proxy_url='http://127.0.0.1:7897'
    observed=[]
    monkeypatch.setattr('newsroom.collect.verify_public_dns',lambda host,**kw:observed.append((host,kw['settings'])))
    monkeypatch.setattr('newsroom.collect.socket.getaddrinfo',lambda *a,**kw:pytest.fail('curated proxy route resolves through independent DoH'))
    assert public_url('https://openai.com/news/rss.xml',curated=True,settings=engine.settings).startswith('https://openai.com')
    assert observed==[('openai.com',engine.settings)]


@pytest.mark.parametrize('data,status', [({'secret':'FAKE-RESPONSE-SECRET'},401),
    ({'choices':[{'message':'FAKE-RESPONSE-SECRET'}]},200), ([],200),
    ({'choices':[{'message':{'content':''}}]},200)])
def test_model_probe_bad_responses_are_safe_single_attempts(local,monkeypatch,data,status):
    client,engine=local
    assert client.put('/api/config',json=real_patch()).status_code==200
    calls=[]
    def post(self,url,**kw):
        calls.append(url)
        return httpx.Response(status,json=data)
    monkeypatch.setattr(httpx.Client,'post',post)
    response=client.request('POST','/api/config/models/writer/test')
    assert response.status_code==200 and not response.json()['ok']
    assert 'FAKE-RESPONSE-SECRET' not in response.text and len(calls)==1


def test_news_http_client_receives_only_news_proxy_and_no_credentials(local,monkeypatch):
    from contextlib import contextmanager
    from newsroom.collect import fetch_document
    client,engine=local
    engine.settings.news_proxy_url='http://127.0.0.1:7897'
    engine.settings.llm_proxy_url='http://127.0.0.1:7898'
    seen=[]
    class Reader:
        def __init__(self,**kw):seen.append(kw)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        @contextmanager
        def stream(self,method,url):
            yield httpx.Response(200,content=b'<article>Real news body</article>',request=httpx.Request(method,url))
    monkeypatch.setattr('newsroom.collect.httpx.Client',Reader)
    monkeypatch.setattr('newsroom.collect.public_url',lambda url,**kw:url)
    result=fetch_document('https://openai.com/news/test',settings=engine.settings)
    assert 'Real news' in result['html'] and seen[0]['proxy']=='http://127.0.0.1:7897'
    assert seen[0]['trust_env'] is False and seen[0]['follow_redirects'] is False
    assert 'Authorization' not in seen[0]['headers'] and 'FAKE-SECRET' not in json.dumps(seen)


@pytest.mark.parametrize('value',['http://remote.example:7897','http://127.0.0.1','http://127.0.0.1:7897/path','socks5://127.0.0.1:7897','http://127.0.0.1:7897?secret=foo'])
def test_invalid_proxy_types_are_rejected(value):
    with pytest.raises(ValueError):proxy_url(value)


def test_credential_save_is_rejected_during_publication_or_capability_request(local):
    client, engine = local
    previous = engine.settings.wechat_app_id
    engine.publish_lock.acquire()
    try:
        result = client.put('/api/config', json={'wechat': {'app_id': 'wxOTHER-ACCOUNT'}})
        assert result.status_code == 409
        assert engine.settings.wechat_app_id == previous
    finally:
        engine.publish_lock.release()
