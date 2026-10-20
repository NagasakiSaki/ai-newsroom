import json
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.connection import WechatConnection
from newsroom.db import Store


@pytest.fixture
def connection(tmp_path):
    settings = Settings(data_dir=tmp_path, admin_token='test-admin', wechat_app_id='wx1234567890abcdef', wechat_app_secret='FAKE-SECRET-for-tests')
    return WechatConnection(settings, Store(tmp_path / 'test.sqlite3'))


def reply(monkeypatch, data=None, status=200):
    requests = []
    def post(self, url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(status, json=data, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    return requests


def test_status_never_requests_wechat_and_hides_credentials(connection, monkeypatch):
    monkeypatch.setattr(httpx.Client, 'post', lambda *a, **kw: pytest.fail('GET status must not test connection'))
    result = connection.status().model_dump()
    assert result['configured'] and result['last_test'] is None
    assert result['appid_masked'] == 'wx…cdef'
    assert 'FAKE-SECRET' not in json.dumps(result)


def test_each_click_requests_only_stable_token_without_forced_refresh(connection, monkeypatch):
    calls = reply(monkeypatch, {'access_token': 'FAKE-TOKEN-never-return', 'expires_in': 7200})
    for _ in range(2):
        result = connection.test().model_dump()
        assert result['ok'] and result['expires_in'] == 7200
    assert len(calls) == 2
    assert all(url == WechatConnection.URL for url, _ in calls)
    assert calls[0][1]['json']['force_refresh'] is False
    assert 'secret' in calls[0][1]['json'] and 'params' not in calls[0][1]
    saved = json.dumps(connection.store.all('integrations'))
    assert 'FAKE-SECRET-for-tests' not in saved and 'FAKE-TOKEN-never-return' not in saved
    assert connection.status().last_test.ok


def test_no_credentials_stops_before_network(connection, monkeypatch):
    connection.settings.wechat_app_secret = ''
    monkeypatch.setattr(httpx.Client, 'post', lambda *a, **kw: pytest.fail('missing credentials'))
    assert connection.test().status == 'not_configured'


@pytest.mark.parametrize('data', [[], {}, {'access_token':'token'}, {'access_token':'token','expires_in':True}, {'access_token':'token','expires_in':0}, {'access_token':'','expires_in':7200}, {'errcode':'0'}])
def test_malformed_responses_are_never_success(connection, monkeypatch, data):
    reply(monkeypatch, data)
    assert connection.test().status == 'invalid_response'


def test_remote_error_keeps_code_but_never_raw_message(connection, monkeypatch):
    reply(monkeypatch, {'errcode':40164,'errmsg':'FAKE-SECRET-for-tests FAKE-TOKEN-never-return sensitive raw data'})
    result = connection.test().model_dump()
    assert not result['ok'] and result['wechat_errcode'] == 40164
    assert 'sensitive' not in json.dumps(result)
    assert 'FAKE-' not in json.dumps(connection.store.all('integrations'))


def test_network_failure_is_sanitized_and_not_retried(connection, monkeypatch):
    calls=[]
    def post(*args, **kwargs):
        calls.append(True)
        raise httpx.ConnectError('FAKE-SECRET-for-tests sensitive request details')
    monkeypatch.setattr(httpx.Client, 'post', post)
    result = connection.test()
    assert result.status == 'network_error' and len(calls) == 1
    assert 'FAKE-' not in json.dumps(connection.store.all('integrations'))


def test_http_error_is_sanitized_and_not_followed_or_retried(connection, monkeypatch):
    calls=reply(monkeypatch, {'secret':'FAKE-SECRET-for-tests'},status=302)
    result=connection.test()
    assert result.status=='http_error' and result.http_status==302 and len(calls)==1


def test_secret_rotation_invalidates_old_test_result(connection, monkeypatch):
    reply(monkeypatch, {'access_token':'token','expires_in':7200})
    connection.test()
    assert connection.status().last_test is not None
    connection.settings.wechat_app_secret='changed-test-secret'
    assert connection.status().last_test is None


@pytest.mark.parametrize('cause,expected', [
    (socket.gaierror('FAKE-SECRET DNS'), 'dns'),
    (ssl.SSLEOFError('FAKE-SECRET TLS'), 'tls'),
    (None, 'connect'),
])
def test_network_errors_identify_stage_without_exposing_exception(connection, monkeypatch, cause, expected):
    def post(*a, **kw):
        raise httpx.ConnectError('FAKE-SECRET sensitive URL') from cause
    monkeypatch.setattr(httpx.Client, 'post', post)
    result = connection.test().model_dump()
    assert result['failure_stage'] == expected and result['status'] == 'network_error'
    assert 'FAKE-SECRET' not in json.dumps(result)


def test_timeout_has_specific_stage(connection, monkeypatch):
    def post(*a, **kw):
        raise httpx.ReadTimeout('FAKE-SECRET timeout')
    monkeypatch.setattr(httpx.Client, 'post', post)
    assert connection.test().failure_stage == 'timeout'


def test_whitelist_error_extracts_only_public_ip(connection, monkeypatch):
    reply(monkeypatch, {'errcode': 40164,
                       'errmsg': 'invalid ip 8.8.8.8 ipv6 ::ffff:8.8.8.8, FAKE-SECRET rid:private'})
    result = connection.test().model_dump()
    assert result['whitelist_ip'] == '8.8.8.8' and result['wechat_errcode'] == 40164
    assert '8.8.8.8' in result['message']
    assert 'FAKE-SECRET' not in json.dumps(result) and 'rid:private' not in json.dumps(result)


@pytest.mark.parametrize('message', [None, {}, 'FAKE-SECRET', 'invalid ip bad-IP',
                                     'invalid ip 127.0.0.1', 'invalid ip 198.18.0.123'])
def test_whitelist_ip_rejects_malformed_private_and_fake_ip(connection, monkeypatch, message):
    reply(monkeypatch, {'errcode': 40164, 'errmsg': message})
    assert connection.test().whitelist_ip is None


def test_invalid_secret_has_specific_action_without_exposing_secret(connection, monkeypatch):
    reply(monkeypatch, {'errcode': 40125, 'errmsg': 'FAKE-SECRET'})
    result = connection.test().model_dump()
    assert result['wechat_errcode'] == 40125 and 'AppSecret' in result['message']
    assert 'FAKE-SECRET' not in json.dumps(result)


def test_concurrent_test_is_rejected_until_first_finishes(connection, monkeypatch):
    entered,release=Event(),Event()
    def post(self,url,**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return httpx.Response(200,json={'access_token':'token','expires_in':7200})
    monkeypatch.setattr(httpx.Client,'post',post)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(connection.test)
        try:
            assert entered.wait(timeout=5)
            assert connection.status().testing
            with pytest.raises(ValueError,match='正在进行'):
                connection.test()
        finally:
            release.set()
        assert future.result(timeout=5).ok
    assert not connection.status().testing


def test_authenticated_post_is_the_only_network_trigger_and_never_creates_drafts(tmp_path,monkeypatch):
    settings=Settings(data_dir=tmp_path,admin_token='test',wechat_app_id='wx1234567890abcdef',wechat_app_secret='FAKE-SECRET-for-tests')
    app=create_app(settings,start_worker=False)
    requests=[]
    def post(self,url,**kwargs):
        requests.append(url)
        assert url==WechatConnection.URL
        return httpx.Response(200,json={'access_token':'token','expires_in':7200})
    # patch Client.post only during explicit calls: TestClient also uses post.
    with TestClient(app) as client:
        assert client.post('/api/wechat/test-connection').status_code==401
        headers={'Authorization':'Bearer test'}
        assert client.get('/api/wechat/status',headers=headers).json()['last_test'] is None
        assert client.get('/api/state',headers=headers).status_code==200
        assert not requests
        with monkeypatch.context() as context:
            context.setattr(httpx.Client,'post',post)
            # generic request avoids patching TestClient.post itself.
            result=client.request('POST','/api/wechat/test-connection',headers=headers)
        assert result.status_code==200 and result.json()['ok']
        assert requests==[WechatConnection.URL]
        assert not app.state.engine.store.all('jobs')
        assert settings.delivery_mode=='local'
