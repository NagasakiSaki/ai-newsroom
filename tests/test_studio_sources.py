import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.config import Settings, sources
from newsroom.db import now
from newsroom.editors import choose_editor
from newsroom.engine import Engine
from newsroom.llm import ModelClient
from newsroom.source_quality import assessment, snapshot, page_dates, validate_claims


@pytest.fixture
def engine(tmp_path):
    return Engine(Settings(data_dir=tmp_path, admin_token='test'))


def source_item():
    source = sources()[0]
    return source, {'id': 'fresh', 'url': 'https://research.google/blog/test/', 'source': source['name'], 'source_id': source['id'], 'published_at': now(), 'date_basis': 'published', 'demo': False}


def test_authority_freshness_and_publication_not_modification():
    source, item = source_item()
    assert assessment(source, item)['auto_eligible']
    assert not assessment(source, {**item, 'date_basis': 'updated_only'})['auto_eligible']
    # 官方一手窗口 72 小时；媒体二手仍是 24 小时。
    assert assessment(source, {**item, 'published_at': (datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()})['auto_eligible']
    assert not assessment(source, {**item, 'published_at': (datetime.now(timezone.utc)-timedelta(hours=73)).isoformat()})['auto_eligible']
    media = next(s for s in sources() if s['tier'] == 'secondary')
    assert not assessment(media, {**item, 'url': 'https://techcrunch.com/2026/10/19/test/', 'published_at': (datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()})['auto_eligible']
    assert not assessment(source, {**item, 'url': 'https://research.google.evil.org/blog/test/'})['auto_eligible']
    community = next(s for s in sources() if s['tier'] == 'discovery')
    assert not assessment(community, {**item, 'url': 'https://huggingface.co/blog/test'})['auto_eligible']
    old = (datetime.now(timezone.utc)-timedelta(days=4)).isoformat()
    with pytest.raises(ValueError):
        snapshot(source, item, f'<meta property="article:published_time" content="{old}"><meta property="article:modified_time" content="{now()}">', 'body')
    assert page_dates('<script type="application/ld+json">{"@type":"Organization","datePublished":"2020-01-01T00:00:00Z"}</script>') == {}


def test_single_source_is_not_independent_verification():
    with pytest.raises(ValueError, match='独立验证'):
        validate_claims({'title': '新闻', 'markdown': '正文'}, {'facts': [{'claim_type': 'independently_verified'}]}, {'tier': 'primary'})
    with pytest.raises(ValueError, match='论文'):
        validate_claims({'title': '新闻', 'markdown': '结论'}, {'facts': []}, {'tier': 'research'})


def test_source_change_blocks_package_and_download(engine, monkeypatch):
    engine.enqueue('pipeline'); engine.work_once()
    d = engine.store.all('drafts')[0]
    d['demo'] = False
    d['source_snapshot'] = {'body_hash': 'old', 'published_at': now(), 'checked_at': '2020-01-01T00:00:00+00:00'}
    engine.store.put('drafts', d)
    # 只有整页哈希变化不算实质改动；页面内容被替换、稿件引文消失时才撤销交稿。
    monkeypatch.setattr(engine, 'prepare_source',
                        lambda item: {**item, 'text': '页面被替换成无关内容', 'source_snapshot': {'body_hash': 'changed', 'body_length': 12}})
    with pytest.raises(ValueError, match='原文已改变'):
        engine.deliver(d['id'], d['revision'])
    blocked = engine.store.get('drafts', d['id'])
    assert blocked['status'] == 'needs_source_review' and blocked['delivery'] is None and blocked['approved_hash'] is None


def test_prompt_and_writer_use_distinct_routes_and_prompt_payload(engine, monkeypatch):
    s=engine.settings
    s.demo=False;s.llm_base_url='https://writer.example/v1';s.llm_api_key='writer-secret';s.llm_model='writer-default'
    s.prompt_base_url='https://planner.example/v1';s.prompt_api_key='planner-secret';s.prompt_model='planner-default'
    editor=choose_editor(engine.store, {'editor_id': 'deep', 'custom_style': '短句解释机制'})
    editor.update(prompt_model='planner-special',writer_model='writer-special')
    run=engine.new_run('test');requests=[]
    prompt=ModelClient.demo('prompt', {'editor': editor})
    article=ModelClient.demo('write', {'editor': editor})
    def post(self,url,**kwargs):
        requests.append((url,kwargs))
        result=prompt if len(requests)==1 else article
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr('newsroom.llm.request_completion',post)
    generated=engine.model.complete('prompt',{'editor':editor},run)
    engine.model.complete('write',{'editor':editor,'writing_prompt':generated},run)
    assert requests[0][0]=='https://planner.example/v1/chat/completions'
    assert requests[0][1]['headers']['Authorization']=='Bearer planner-secret'
    assert requests[1][1]['headers']['Authorization']=='Bearer writer-secret'
    assert requests[0][1]['json']['model']=='planner-special'
    assert requests[1][1]['json']['model']=='writer-special'
    messages=requests[1][1]['json']['messages']
    assert generated['system_prompt'] in messages[1]['content']
    assert messages[-1]['content']==generated['user_prompt']
    assert '不得补写证据以外' in messages[0]['content']
    assert engine.store.get('runs',run['id'])['calls']==2


def test_missing_planner_model_never_silently_reuses_writer(engine):
    engine.settings.demo=False;engine.settings.llm_base_url='https://writer.example';engine.settings.llm_api_key='secret';engine.settings.llm_model='writer'
    with pytest.raises(ValueError, match='模型 ID'):
        engine.model.complete('prompt',{'editor':choose_editor(engine.store)},engine.new_run('test'))


def test_composition_api_persists_style_snapshot_prompt_and_rewrites(tmp_path):
    app=create_app(Settings(data_dir=tmp_path,admin_token='test'),start_worker=False)
    engine=app.state.engine
    headers={'Authorization':'Bearer test'}
    with TestClient(app) as client:
        client.post('/api/run',headers=headers);engine.work_once()
        d=engine.store.all('drafts')[0]
        result=client.post('/api/compose',headers=headers,json={'story_id':d['story_id'],'draft_id':d['id'],'revision':d['revision'],'editor_id':'business','custom_style':'面向小公司负责人，先讲成本边界'})
        assert result.status_code==200
        engine.work_once();new=engine.store.get('drafts',d['id'])
        assert new['revision']==d['revision']+1 and new['status']=='pending_human'
        assert new['editor']['name']=='策言'
        assert '成本边界' in new['writing_prompt']['system_prompt']
        calls=[c['stage'] for c in engine.store.all('calls') if c['run_id']==new['run_id']]
        assert 'prompt' in calls and 'write' in calls
        assert '企业选型' in new['article']['title']
        assert (tmp_path/'exports'/d['id']/f"v{new['revision']}"/'writing-prompt.json').exists()
        data=client.get('/api/state',headers=headers).json()
        assert {'relay','flash','rigor','common'} <= {e['id'] for e in data['editors']}
        assert all('api_key' not in p for p in data['model_profiles'])
        assert client.post('/api/compose',headers=headers,json={'story_id':d['story_id'],'draft_id':d['id'],'revision':1}).status_code==409


def test_collection_job_independent_and_preserves_original_time(engine, monkeypatch):
    source,item=source_item()
    item.update(title='Fresh news',status='new')
    def collect(*args,**kwargs):
        kwargs['health']({'id':source['id'],'status':'ok','last_success_at':now()})
        return [item]
    monkeypatch.setattr('newsroom.collect.collect',collect)
    engine.collection_tick();engine.work_once()
    assert not engine.store.all('calls') and not engine.store.all('drafts')
    original=engine.store.get('stories',item['id'])['published_at']
    engine.ingest([{**item,'published_at':(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()}])
    assert engine.store.get('stories',item['id'])['published_at']==original
    assert engine.store.all('source_health')[0]['status']=='ok'
    assert engine.get_schedule()['times']==['09:00','18:00']


def test_curated_dns_exception_is_narrow_and_preserves_feed_slash(monkeypatch):
    from newsroom.collect import public_url
    def resolve(*args, **kwargs):
        return [(2, 1, 6, '', ('198.18.0.1', 443))]
    monkeypatch.setattr('newsroom.collect.socket.getaddrinfo', resolve)
    checked=[]
    monkeypatch.setattr('newsroom.collect.verify_public_dns', lambda host: checked.append(host))
    assert public_url('https://research.google/blog/rss/', curated=True).endswith('/rss/')
    assert checked == ['research.google']
    with pytest.raises(ValueError):
        public_url('https://research.google/blog/rss/')
    with pytest.raises(ValueError):
        public_url('https://unregistered.example/feed', curated=True)
    monkeypatch.setattr('newsroom.collect.socket.getaddrinfo', lambda *a,**kw:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError):
        public_url('https://research.google/blog/rss/',curated=True)


def test_expired_source_and_failed_refetch_revoke_approval(engine, monkeypatch):
    engine.enqueue('pipeline');engine.work_once()
    d=engine.store.all('drafts')[0]
    d.update(demo=False,approved_hash='stale',status='ready_to_publish',source_snapshot={'body_hash':'old','published_at':(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat(),'checked_at':now(),'max_age_hours':24})
    engine.store.put('drafts',d)
    monkeypatch.setattr(engine,'prepare_source',lambda _:(_ for _ in ()).throw(ValueError('原始发布时间过期')))
    with pytest.raises(ValueError,match='过期'):
        engine.verify_source(d)
    result=engine.store.get('drafts',d['id'])
    assert result['status']=='needs_source_review' and result['approved_hash'] is None


def test_only_updated_rss_is_never_a_fresh_publication(monkeypatch):
    from newsroom.collect import collect
    source,item=source_item()
    xml=f'<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title><entry><title>Update only</title><link href="{item["url"]}"/><updated>{now()}</updated></entry></feed>'
    monkeypatch.setattr('newsroom.collect.fetch_text',lambda *a,**kw:xml)
    items=collect([source],72,lambda *a:None)
    assert len(items)==1 and items[0]['date_basis']=='updated_only'
    assert not items[0]['source_report']['auto_eligible']
