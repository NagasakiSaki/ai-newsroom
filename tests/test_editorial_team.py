import copy
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from newsroom.app import create_app
from newsroom.collect import parse_article_index
from newsroom.config import Settings, sources
from newsroom.editors import choose_editor, resolve_profile
from newsroom.engine import Engine
from newsroom.models import Article
from newsroom.render import render_article
from newsroom.runtime_config import RuntimeConfigPatch, candidate, load_saved, persist
from newsroom.editorial_team import asset_file, image_candidates, validate_visual_plan
from tests.test_publication import approved, enable, FakeWechat


def engine_at(path):
    return Engine(Settings(data_dir=path, admin_token='test'))


def test_relay_uses_three_bound_editors_and_includes_assets_in_portable_bundle(tmp_path):
    engine = engine_at(tmp_path)
    editor = choose_editor(engine.store, {'editor_id': 'relay'})
    engine.enqueue('pipeline', payload={'editor': editor})
    engine.work_once()
    draft = engine.store.all('drafts')[0]
    assert draft['status'] == 'pending_human'
    assert [p['editor']['id'] for p in draft['team_contributions']] == ['flash', 'rigor', 'common']
    calls = engine.store.all('calls')
    assert sum(c['stage'] == 'write' for c in calls) == 3
    assert sum(c['stage'] == 'prompt' for c in calls) == 3
    assert any(c['stage'] == 'plan' for c in calls) and any(c['stage'] == 'visual' for c in calls)
    asset = draft['visuals']['assets'][0]
    assert asset_file(tmp_path, asset).read_bytes().startswith(b'\x89PNG')
    archive = tmp_path / 'exports' / draft['id'] / 'v1' / 'handoff.zip'
    with ZipFile(archive) as package:
        assert 'media/' + asset['file'] in package.namelist()
        assert b'/media/' not in package.read('article.html')
        assert ('media/' + asset['file']).encode() in package.read('article.html')
        assert 'editorial-plan.json' in package.namelist()


def test_public_article_has_sources_without_internal_audit_appendices(tmp_path):
    engine = engine_at(tmp_path); engine.enqueue('pipeline'); engine.work_once()
    draft = engine.store.all('drafts')[0]
    draft['source_snapshot'] = {'warnings': ['INTERNAL_SOURCE_DOUBT']}
    draft['crosscheck'] = {'state': 'primary_announcement', 'summary': 'INTERNAL_AUDIT_SUMMARY', 'sources': []}
    html = render_article(draft)
    assert '参考来源' in html and '<img' in html
    assert 'INTERNAL_SOURCE_DOUBT' not in html and 'INTERNAL_AUDIT_SUMMARY' not in html
    assert '未找到其他机构' not in html and '[F1]' not in html


def test_unlimited_body_and_output_payload_and_length_continuation(tmp_path, monkeypatch):
    engine = engine_at(tmp_path)
    engine.settings.demo = False
    engine.settings.llm_base_url='https://writer.example/v1'; engine.settings.llm_api_key='fake';engine.settings.llm_model='writer'
    engine.settings.max_output_tokens = 0
    article = {'title':'长稿','digest':'摘要','markdown':'有证据的长文[F1]。' * 4000}
    Article.model_validate(article)
    raw=json.dumps(article,ensure_ascii=False); cut=len(raw)//2
    seen=[]
    def completion(self,url,**kw):
        seen.append(copy.deepcopy(kw['json']))
        return httpx.Response(200,json={'choices':[{'finish_reason':'length' if len(seen)==1 else 'stop',
            'message':{'content':raw[:cut] if len(seen)==1 else raw[cut:]}}], 'usage':{'total_tokens':10}},request=httpx.Request('POST',url))
    monkeypatch.setattr('newsroom.llm.request_completion',completion)
    from newsroom.llm import ModelClient
    prompt=ModelClient.demo('prompt',{'editor':choose_editor(engine.store)})
    result=engine.model.complete('write',{'writing_prompt':prompt},engine.new_run('test'))
    assert result==article and len(seen)==2 and all('max_tokens' not in p for p in seen)
    assert any(c['outcome']=='continuation' for c in engine.store.all('calls'))


def test_daily_quota_zero_does_not_block_after_more_than_three_articles(tmp_path):
    engine=engine_at(tmp_path)
    for n in range(5):
        engine.store.put('drafts', {'id':'old-'+str(n),'story_id':'previous-'+str(n),'created_at':__import__('newsroom.db',fromlist=['now']).now(),'demo':True})
    engine.enqueue('pipeline');engine.work_once()
    assert len(engine.store.all('drafts'))==6


def test_visual_model_can_use_independent_credentials_and_dpapi_persistence(tmp_path):
    settings=Settings(data_dir=tmp_path,admin_token='test',llm_base_url='https://writer.example',llm_api_key='writer-key',llm_model='writer')
    updated=candidate(settings,RuntimeConfigPatch(models={'visual':{'base_url':'https://visual.example/v1','api_key':'visual-key','model':'layout'}}))
    assert resolve_profile(updated,'visual')['api_key']=='visual-key'
    assert resolve_profile(updated,'writer')['api_key']=='writer-key'
    persist(updated)
    assert load_saved(tmp_path)['visual_model']=='layout'
    assert candidate(updated,RuntimeConfigPatch(max_output_tokens=0)).max_output_tokens==0


def test_visual_plan_rejects_unknown_facts_and_asset_tampering(tmp_path):
    with pytest.raises(ValueError):
        validate_visual_plan({'cards':[{'fact_ids':['F9']}],'reference_image_ids':[]},{'facts':[{'id':'F1'}]},[])
    path=tmp_path/'media';path.mkdir(); raw=b'changed';ident=hashlib.sha256(b'original').hexdigest()
    (path/(ident+'.png')).write_bytes(raw)
    with pytest.raises(ValueError,match='变化'):
        asset_file(tmp_path,{'id':ident,'sha256':ident})


def test_image_candidates_are_references_and_never_accept_script_urls():
    images=image_candidates('<meta property="og:image" content="https://images.example/cover.png"><article><img src="javascript:evil()"></article>','https://vendor.example/post')
    assert len(images)==1 and images[0]['usage']=='reference_only'


def test_official_index_requires_real_date_not_current_fetch_time(monkeypatch):
    source={'name':'国内官方','url':'https://vendor.example/news','allowed_hosts':['vendor.example'],'article_prefixes':['/news/']}
    feed=parse_article_index('<a href="/news/release"><h2>模型发布</h2>2026年10月19日</a>',source)
    assert feed.entries[0]['published_parsed'][:3]==(2026,10,19)
    monkeypatch.setattr('newsroom.collect.fetch_text',lambda *a,**kw:'<h1>没有日期</h1>')
    with pytest.raises(ValueError,match='发布日期'):
        parse_article_index('<a href="/news/undated">没有日期的发布</a>',source)


def test_registered_domestic_official_and_benchmark_sources():
    registered=sources()
    assert len([s for s in registered if s.get('region')=='CN' and s['tier']=='primary' and s['enabled']])>=3
    assert len([s for s in registered if s['kind']=='benchmark' and s['enabled']])>=2


def test_inline_png_upload_is_guarded_cached_and_embedded_as_wechat_url(tmp_path):
    engine=Engine(Settings(data_dir=tmp_path,admin_token='test',wechat_app_id='wxFAKE123',wechat_app_secret='fake'))
    engine.wechat=engine.fake=FakeWechat()
    draft=approved(engine)
    engine.prepare_visuals(draft,engine.new_run('visual-test'))
    draft.update(status='pending_human', approved_hash=None)
    engine.store.put('drafts',draft)
    engine.approve(draft['id'],draft['revision'],'test',True)
    draft=engine.store.get('drafts',draft['id'])
    enable(engine)
    publication=engine.create_publication(draft['id'],draft['revision']);engine.work_once()
    assert engine.get_publication(publication['id'])['status']=='accepted'
    assert [c[0] for c in engine.fake.calls]==['media/uploadimg','draft/add','freepublish/submit']
    payload=next(c[1] for c in engine.fake.calls if c[0]=='draft/add')
    assert 'https://mmbiz.qpic.cn/fake-inline.png' in payload['content'] and '/media/' not in payload['content']
