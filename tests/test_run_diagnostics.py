import copy
import json
from contextlib import contextmanager

import httpx
import pytest

from newsroom.config import Settings
from newsroom.engine import Engine
from newsroom.llm import request_completion, ModelDeadlineExceeded, ModelResponseTooLarge
from newsroom.models import OutputCheckError
from newsroom.render import validate_evidence
from newsroom.collect import demo_items
from tests.test_crosscheck import fixtures


@pytest.fixture
def engine(tmp_path):
    return Engine(Settings(data_dir=tmp_path,admin_token='fake-admin',llm_base_url='https://models.example/v1',llm_api_key='FAKE-KEY',llm_model='model',prompt_model='planner'))


def fake_completion(monkeypatch, outputs):
    calls=[]
    def request(client,url,**kwargs):
        calls.append(copy.deepcopy(kwargs))
        output=outputs[min(len(calls)-1,len(outputs)-1)]
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}],
            'usage':{'total_tokens':12,'prompt_tokens':8,'completion_tokens':4,'unexpected_secret':'FAKE-RAW-USAGE'}})
    monkeypatch.setattr('newsroom.llm.request_completion',request)
    monkeypatch.setattr('newsroom.llm.time.sleep',lambda _:None)
    return calls


def test_crosscheck_bad_quote_retries_with_safe_feedback_before_acceptance(engine,monkeypatch):
    engine.settings.demo=False
    a,b,e,good=fixtures()
    bad=copy.deepcopy(good);bad['sources'][0]['comparisons'][0]['quote']='FAKE-OUTPUT-that-is-not-an-actual-quote'
    calls=fake_completion(monkeypatch,[bad,good]);run=engine.new_run('test')
    result=engine.model.complete('crosscheck',{},run,validator=lambda output:engine.validate_crosscheck(a,e,[b],output))
    assert result==good and len(calls)==2
    records=list(reversed(engine.store.all('calls')))
    assert records[0]['outcome']=='validation_failed' and records[1]['outcome']=='ok'
    assert records[0]['diagnostic']['code']=='comparison_quote_mismatch'
    assert '.F1.quote' in records[0]['diagnostic']['location']
    assert 'comparison_quote_mismatch' in calls[1]['json']['messages'][-1]['content']
    assert 'FAKE-OUTPUT' not in json.dumps(records) and 'FAKE-RAW-USAGE' not in json.dumps(records)
    summary=engine.store.get('runs',run['id'])
    assert summary['calls']==2 and summary['token_usage']['total_tokens']==24 and 'active_model_call' not in summary


def test_invalid_quote_never_passes_after_three_attempts(engine,monkeypatch):
    engine.settings.demo=False
    a,b,e,bad=fixtures();bad['sources'][0]['comparisons'][0]['quote']='a fabricated quote not in the source'
    calls=fake_completion(monkeypatch,[bad]);run=engine.new_run('test')
    with pytest.raises(ValueError,match='comparison_quote_mismatch'):
        engine.model.complete('crosscheck',{},run,validator=lambda output:engine.validate_crosscheck(a,e,[b],output))
    assert len(calls)==3 and not engine.store.all('drafts')
    assert all(call['outcome']=='validation_failed' for call in engine.store.all('calls'))


def test_semantic_retry_shares_original_model_budget(engine,monkeypatch):
    engine.settings.demo=False;engine.settings.max_calls=1
    a,b,e,bad=fixtures();bad['sources'][0]['comparisons'][0]['quote']='a fabricated quote not in the source'
    calls=fake_completion(monkeypatch,[bad]);run=engine.new_run('test')
    with pytest.raises(ValueError,match='预算'):
        engine.model.complete('crosscheck',{},run,validator=lambda output:engine.validate_crosscheck(a,e,[b],output))
    assert len(calls)==1


def test_extract_validates_original_quote_in_same_retry_loop(engine,monkeypatch):
    from newsroom.llm import ModelClient
    engine.settings.demo=False
    news=demo_items()[0]
    good=ModelClient.demo('extract',{'news':news});bad=copy.deepcopy(good);bad['facts'][0]['quote']='a fabricated original source quote'
    calls=fake_completion(monkeypatch,[bad,good]);run=engine.new_run('test')
    result=engine.model.complete('extract',{'news':news},run,validator=lambda output:validate_evidence(output,news))
    from newsroom.models import Evidence
    assert len(calls)==2 and result==Evidence.model_validate(good).model_dump()


def test_only_irrelevant_corroboration_is_not_treated_as_model_failure(engine,monkeypatch):
    engine.settings.demo=False
    a,b,e,result=fixtures();result['sources'][0]['same_event']=False
    calls=fake_completion(monkeypatch,[result]);run=engine.new_run('test')
    engine.model.complete('crosscheck',{},run,validator=lambda output:engine.validate_crosscheck(a,e,[b],output))
    assert len(calls)==1 and engine.store.all('calls')[0]['outcome']=='ok'
    # 不相关的对照不能计为旁证；一手主线仍按官方公告规则处理。
    report=engine.validate_crosscheck(a,e,[b],result)
    assert report['state']=='primary_announcement' and not report['sources'][0]['counted_as_corroboration']


def test_collection_completion_trigger_and_job_run_link(engine,monkeypatch):
    monkeypatch.setattr('newsroom.collect.collect',lambda *a,**kw:[])
    engine.collection_tick();engine.work_once()
    job=engine.store.all('jobs')[0];run=engine.store.all('runs')[0]
    assert job['trigger']=='collection_schedule' and run['trigger']=='collection_schedule'
    assert job['run_id']==run['id'] and run['job_id']==job['id'] and job['run_status']=='done'
    assert run['stage']=='finished' and '采集结束' in run['message'] and '正在' not in run['message']


def test_pipeline_no_news_never_promises_pending_review(engine):
    engine.enqueue('pipeline');engine.work_once()
    engine.enqueue('pipeline');engine.work_once()
    run=engine.store.all('runs')[0]
    assert run['status']=='no_news' and run['generated']==0 and '等待 AI' not in run['message']
    assert '没有符合' in run['message']


def test_pipeline_hold_is_a_distinct_result_and_not_a_successful_article(engine,monkeypatch):
    from newsroom.crosscheck import CorroborationPending
    def hold(*args):raise CorroborationPending('缺少同事件旁证')
    monkeypatch.setattr(engine,'crosscheck_story',hold)
    job=engine.enqueue('pipeline');engine.work_once()
    run=engine.store.all('runs')[0]
    assert run['status']=='awaiting_corroboration' and run['awaiting_corroboration']==1
    assert run['generated']==0 and run['failures']==0 and not engine.store.all('drafts')
    assert '没有进入写稿' in run['message']
    assert engine.store.get('jobs',job['id'])['run_status']=='awaiting_corroboration'


def test_all_article_failures_are_failed_instead_of_partial_success(engine,monkeypatch):
    def invalid(*args):raise OutputCheckError('对照摘录并非原文连续片段','comparison_quote_mismatch')
    monkeypatch.setattr(engine,'crosscheck_story',invalid)
    engine.enqueue('pipeline');engine.work_once()
    run=engine.store.all('runs')[0]
    assert run['status']=='failed' and run['generated']==0 and run['failures']==1
    assert '采编失败' in run['message'] and '等待 AI' not in run['message']


def test_schema_extra_field_is_not_echoed_in_diagnostics(engine,monkeypatch):
    engine.settings.demo=False
    output={'relevance':80,'impact':80,'novelty':80,'timeliness':80,'reliability':80,'reason':'fine','FAKE-SECRET-FIELD':1}
    fake_completion(monkeypatch,[output]);run=engine.new_run('test')
    with pytest.raises(ValueError):engine.model.complete('score',{},run)
    records=json.dumps(engine.store.all('calls'))
    assert 'FAKE-SECRET-FIELD' not in records and 'unexpected_field' in records


def test_terminal_model_http_error_is_recorded_once(engine,monkeypatch):
    engine.settings.demo=False;observed=[]
    def reject(client,url,**kwargs):
        observed.append(url);return httpx.Response(401)
    monkeypatch.setattr('newsroom.llm.request_completion',reject)
    with pytest.raises(ValueError,match='HTTP 401'):engine.model.complete('score',{},engine.new_run('test'))
    assert len(observed)==1 and engine.store.all('calls')[0]['diagnostic']['http_status']==401


class StreamingClient:
    def __init__(self,chunks):self.chunks=chunks;self.closed=False
    @contextmanager
    def stream(self,method,url,**kwargs):
        outer=self
        class Response:
            status_code=200
            request=httpx.Request('POST','https://models.example/v1/chat/completions')
            def iter_bytes(self):yield from outer.chunks
        try:yield Response()
        finally:self.closed=True


def test_model_keepalive_chunks_cannot_extend_response_without_limit(monkeypatch):
    tick=iter([0,90,181]);monkeypatch.setattr('newsroom.llm.time.monotonic',lambda:next(tick))
    client=StreamingClient([b' ',b' '])
    with pytest.raises(ModelDeadlineExceeded):request_completion(client,'https://models.example/v1/chat/completions')
    assert client.closed


def test_model_response_size_limit_closes_stream():
    client=StreamingClient([b' ' * 2_000_001])
    with pytest.raises(ModelResponseTooLarge):request_completion(client,'https://models.example/v1/chat/completions')
    assert client.closed


def test_model_stream_reads_real_mock_transport_and_checks_http_errors():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,content=b'{"valid":true}')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert request_completion(client,'https://models.example/v1/chat/completions',json={}).json()=={'valid':True}
    assert len(requests)==1
