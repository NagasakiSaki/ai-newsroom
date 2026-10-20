import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import launcher
from newsroom.app import create_app
from newsroom.config import Settings
from newsroom.desktop_login import DesktopTickets


def test_desktop_ticket_is_short_lived_one_use_and_stored_as_hash(monkeypatch):
    clock=[100.0]
    monkeypatch.setattr('newsroom.desktop_login.time.monotonic',lambda:clock[0])
    tickets=DesktopTickets()
    issued=tickets.issue()
    assert issued['expires_in']==60 and len(issued['ticket'])>=32
    assert issued['ticket'] not in tickets.pending
    assert hashlib.sha256(issued['ticket'].encode()).hexdigest() in tickets.pending
    assert tickets.consume(issued['ticket'])
    assert not tickets.consume(issued['ticket'])
    expiring=tickets.issue()['ticket']
    clock[0]=160.0
    assert not tickets.consume(expiring)
    assert not tickets.consume('invented-ticket')


def test_only_one_concurrent_exchange_can_succeed():
    tickets=DesktopTickets();ticket=tickets.issue()['ticket']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(tickets.consume,[ticket]*4))
    assert results.count(True)==1


def test_pending_ticket_storage_is_bounded():
    tickets=DesktopTickets()
    oldest=tickets.issue()['ticket']
    for _ in range(12):tickets.issue()
    assert len(tickets.pending)==8 and not tickets.consume(oldest)


def test_desktop_login_requires_issued_ticket_and_does_not_weaken_api_auth(tmp_path):
    app=create_app(Settings(data_dir=tmp_path,admin_token='fake-local-admin'),start_worker=False)
    with TestClient(app) as client:
        assert client.post('/api/desktop/login-ticket').status_code==401
        assert client.post('/api/desktop/login',json={'ticket':'x'*43}).status_code==401
        assert client.get('/api/state').status_code==401
        issued=client.post('/api/desktop/login-ticket',headers={'Authorization':'Bearer fake-local-admin'}).json()
        assert 'fake-local-admin' not in json.dumps(issued)
        login=client.post('/api/desktop/login',json={'ticket':issued['ticket']})
        assert login.status_code==200 and login.json()=={'token':'fake-local-admin'}
        assert login.headers['cache-control']=='no-store'
        assert client.get('/api/state',headers={'Authorization':'Bearer '+login.json()['token']}).status_code==200
        assert client.post('/api/desktop/login',json={'ticket':issued['ticket']}).status_code==401
        assert client.get('/api/desktop/login/other').status_code==401
        assert not app.state.engine.store.all('jobs')
        assert not app.state.engine.store.all('integrations')
        assert not app.state.engine.store.events()
        schema=client.get('/openapi.json').json()
        assert schema['paths']['/api/desktop/login']['post']['security']==[]
        assert schema['paths']['/api/desktop/login-ticket']['post']['security']==[{'AdminBearer':[]}]


def test_restart_does_not_keep_login_tickets(tmp_path):
    settings=Settings(data_dir=tmp_path,admin_token='fake-admin')
    with TestClient(create_app(settings,start_worker=False)) as client:
        ticket=client.post('/api/desktop/login-ticket',headers={'Authorization':'Bearer fake-admin'}).json()['ticket']
    with TestClient(create_app(settings,start_worker=False)) as client:
        assert client.post('/api/desktop/login',json={'ticket':ticket}).status_code==401


def test_launcher_uses_short_ticket_in_fragment_never_admin_in_url(monkeypatch):
    requests=[]
    class LocalHTTP:
        def open(self,request,timeout):
            requests.append(request)
            return io.BytesIO(json.dumps({'ticket':'short-lived-fake-ticket','expires_in':60}).encode())
    monkeypatch.setattr(launcher,'HTTP',LocalHTTP())
    url=launcher.browser_login_url('fake-long-lived-admin')
    assert url=='http://127.0.0.1:8088/#desktop-login=short-lived-fake-ticket'
    assert 'fake-long-lived-admin' not in url
    assert requests[0].get_method()=='POST'
    assert requests[0].get_header('Authorization')=='Bearer fake-long-lived-admin'


def test_launcher_does_not_echo_private_request_error(monkeypatch):
    class BrokenHTTP:
        def open(self,*args,**kwargs):
            raise OSError('fake-long-lived-admin should not be exposed')
    monkeypatch.setattr(launcher,'HTTP',BrokenHTTP())
    with pytest.raises(RuntimeError) as error:
        launcher.browser_login_url('fake-long-lived-admin')
    assert 'fake-long-lived-admin' not in str(error.value)
