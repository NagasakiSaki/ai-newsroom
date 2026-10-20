import base64
import io
import json
import os
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from newsroom.config import Settings
from newsroom.image_protocol import image_endpoint, image_request, image_result
from newsroom.image_service import generate_image
from newsroom.runtime_config import RuntimeConfigPatch, candidate, load_saved, persist, protect, public_config
from test_draft_workflow import configured


@pytest.mark.parametrize('base', [
    'https://api.minimax.cn', 'https://api.minimax.cn/v1',
    'https://api.minimax.cn/v1/image_generation', 'https://api.minimax.cn/v1/image_generation/',
    'https://api.minimax.cn/v1/images/generations',
    'https://api.minimax.cn/v1/image_generation/images/generations',
])
def test_minimax_endpoint_never_appends_compatible_path(base):
    assert image_endpoint(base) == ('minimax', 'https://api.minimax.cn/v1/image_generation')


def test_compatible_gateway_and_exact_host_detection():
    assert image_endpoint('https://images.example/v1/images/generations') == ('openai_compatible','https://images.example/v1/images/generations')
    assert image_endpoint('https://api.minimax.cn.evil.example/v1')[0] == 'openai_compatible'
    assert image_endpoint('https://api.minimax.io/v1')[1] == 'https://api.minimax.io/v1/image_generation'
    with pytest.raises(ValueError, match='MiniMax 原生'):
        image_endpoint('https://api.minimax.cn/v1/image_generation', 'openai_compatible')
    with pytest.raises(ValueError):
        image_endpoint('https://api.minimax.cn/v1?api_key=not-allowed')


def test_native_parameters_and_prompt_limit_are_distinct_from_compatible(tmp_path):
    s=Settings(data_dir=tmp_path,image_base_url='https://api.minimax.cn/v1/image_generation',image_model='image-01')
    provider,endpoint,payload=image_request(s,'A'*2000)
    assert provider=='minimax' and endpoint.endswith('/v1/image_generation')
    assert len(payload['prompt'])==1500 and payload['response_format']=='base64' and payload['n']==1
    assert payload['width']==payload['height']==1024 and 'size'not in payload
    assert image_request(replace(s,image_size='auto'),'scene')[2]['aspect_ratio']=='16:9'
    assert image_request(replace(s,image_model='image-01-live',image_size='1280x720'),'scene')[2]['aspect_ratio']=='16:9'
    with pytest.raises(ValueError,match='8 的倍数'):
        image_request(replace(s,image_size='1023x1024'),'scene')


def encoded_picture():
    output=io.BytesIO();Image.new('RGB',(900,500),'#674298').save(output,format='PNG')
    return base64.b64encode(output.getvalue()).decode()


def mock_native(engine, monkeypatch, result):
    engine.settings.demo=False
    engine.settings.image_enabled=True
    engine.settings.image_base_url='https://api.minimax.cn/v1/image_generation'
    engine.settings.image_api_key='PRIVATE-IMAGE-KEY'
    engine.settings.image_model='image-01'
    requests=[];original=httpx.Client
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json=result)
    monkeypatch.setattr('newsroom.image_service.httpx.Client',lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    return requests


def test_minimax_base64_success_is_saved_and_cached(tmp_path,monkeypatch):
    engine,draft=configured(tmp_path)
    requests=mock_native(engine,monkeypatch,{'base_resp':{'status_code':0},'data':{'image_base64':[encoded_picture()]}})
    asset=generate_image(engine,draft)
    assert asset and asset['generated'] and asset['image_provider']=='minimax'
    assert generate_image(engine,draft)['id']==asset['id'] and len(requests)==1
    assert str(requests[0].url)=='https://api.minimax.cn/v1/image_generation'
    assert 'size'not in json.loads(requests[0].content)
    record=engine.store.all('image_calls')[0]
    assert record['outcome']=='ok' and record['http_status']==200
    assert 'PRIVATE-IMAGE-KEY'not in json.dumps(record)


@pytest.mark.parametrize('body,expected',[
    ({'base_resp':{'status_code':1008,'status_msg':'PRIVATE-RAW-RESPONSE'},'data':{}},'余额不足'),
    ({'base_resp':{'status_code':1004},'data':{}},'鉴权失败'),
    ({'base_resp':{'status_code':0},'data':{}},'未返回'),
    ({'data':{'image_base64':[encoded_picture()]}},'业务状态'),
])
def test_native_business_errors_are_safe_and_not_retried(tmp_path,monkeypatch,body,expected):
    engine,draft=configured(tmp_path);requests=mock_native(engine,monkeypatch,body)
    assert generate_image(engine,draft) is None
    assert generate_image(engine,draft) is None and len(requests)==1
    record=engine.store.all('image_calls')[0]
    assert expected in record['error'] and 'PRIVATE-RAW-RESPONSE'not in json.dumps(record)
    if body.get('base_resp',{}).get('status_code'):
        assert record['provider_code']==body['base_resp']['status_code']


def test_native_url_response_download_has_no_api_authorization(tmp_path,monkeypatch):
    engine,draft=configured(tmp_path)
    mock_native(engine,monkeypatch,{'base_resp':{'status_code':0},'data':{'image_urls':['https://cdn.example/generated.png']}})
    seen=[]
    def fetch(url,source,settings):
        seen.append(url)
        return base64.b64decode(encoded_picture()),url
    monkeypatch.setattr('newsroom.image_service.fetch_image',fetch)
    assert generate_image(engine,draft)['generated']
    assert seen==['https://cdn.example/generated.png']


def test_protocol_fix_does_not_inherit_old_404_cooldown(tmp_path,monkeypatch):
    import hashlib
    from newsroom.db import now
    engine,draft=configured(tmp_path)
    requests=mock_native(engine,monkeypatch,{'base_resp':{'status_code':0},'data':{'image_base64':[encoded_picture()]}})
    prompt=('Create a polished conceptual editorial illustration for a modern Chinese technology feature. '
        'No text, logos, charts, numerical claims, watermarks, or depiction of a real reported event as a photograph. '
        'Strong central subject, cinematic lighting, clean composition, usable as a wide cover crop. '
        'Topic: '+draft['article']['title']+'\nBackground: '+draft['evidence']['summary'])
    old=hashlib.sha256(json.dumps([engine.settings.image_base_url,engine.settings.image_model,engine.settings.image_size,prompt],ensure_ascii=False).encode()).hexdigest()
    engine.store.put('generated_images',{'id':old,'failed_at':now(),'credential':hashlib.sha256(engine.settings.image_api_key.encode()).hexdigest(),'error':'HTTP 404'})
    assert generate_image(engine,draft)['generated'] and len(requests)==1


def test_saved_old_configuration_migrates_without_new_key_and_reports_endpoint(tmp_path):
    engine,draft=configured(tmp_path)
    settings=candidate(engine.settings,RuntimeConfigPatch(image_generation={'enabled':True,'base_url':'https://api.minimax.cn/v1/image_generation','model':'image-01','api_key':'SECRET'}))
    persist(settings)
    path=tmp_path/'runtime-settings.json';envelope=json.loads(path.read_text())
    if os.name=='nt':
        values=json.loads(protect(base64.b64decode(envelope['payload']),decrypt=True));values.pop('image_provider')
        envelope['payload']=base64.b64encode(protect(json.dumps(values).encode())).decode()
    else:envelope['values'].pop('image_provider')
    path.write_text(json.dumps(envelope))
    saved=load_saved(tmp_path)
    loaded=replace(engine.settings,**saved)
    config=public_config(loaded,engine.store)['image_generation']
    assert config['provider']=='auto' and config['resolved_provider']=='minimax'
    assert config['endpoint']=='https://api.minimax.cn/v1/image_generation'
    assert 'SECRET'not in json.dumps(config)
    persist(replace(loaded,image_provider='minimax'))
    assert load_saved(tmp_path)['image_provider']=='minimax'
