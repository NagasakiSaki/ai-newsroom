import copy
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from newsroom.collect import parse_news_html
from newsroom.config import Settings, sources
from newsroom.crosscheck import CorroborationPending, near_copy
from newsroom.db import now
from newsroom.engine import Engine
from newsroom.source_quality import assessment


@pytest.fixture
def engine(tmp_path):
    return Engine(Settings(data_dir=tmp_path, admin_token='test'))


def story(ident, source_id, title, text):
    source = next(s for s in sources() if s['id'] == source_id)
    path = {'google-research': '/blog/test/', 'google-deepmind': '/blog/test/', 'ars-ai': '/ai/2026/10/test/', 'techcrunch-ai': '/2026/10/19/test/'}[source_id]
    from urllib.parse import urlsplit
    url = 'https://' + urlsplit(source['url']).hostname + path
    return {'id': ident, 'source_id': source_id, 'source': source['name'], 'title': title, 'url': url,
            'published_at': now(), 'collected_at': now(), 'date_basis': 'published', 'demo': False, 'status': 'new',
            'text': text, 'excerpt': title, 'source_snapshot': {'body_hash': hashlib.sha256(text.encode()).hexdigest(), 'tier': source['tier'], 'published_at': now(), 'checked_at': now(), 'max_age_hours': 24, 'state': 'eligible'}}


def fixtures():
    anchor=story('anchor','google-research','Gemini 5 releases public document citations','The release introduces public document citations for Gemini 5. The vendor reports results on its internal tests only. ' * 2)
    other=story('other','ars-ai','Google Gemini 5 adds document citations','Our reporter checked the public release notes. Gemini 5 has launched with document citations. This is separate reporting and the performance was not tested by this newsroom.')
    evidence={'facts':[{'id':'F1','text':'Gemini 5 已发布文档出处功能','quote':'The release introduces public document citations','source_url':anchor['url']}], 'summary':'新功能发布','uncertainties':['效果未验证']}
    result={'anchor_relationship':'primary_material','sources':[{'story_id':'other','same_event':True,'confidence':.98,'relationship':'original_reporting','reason':'同一版本与发布事件','comparisons':[{'fact_id':'F1','judgment':'supports','quote':'Gemini 5 has launched with document citations.','explanation':'双方均说明发布了文档出处功能，没有验证性能。'}]}],'summary':'发布行为一致'}
    return anchor,other,evidence,result


def test_multi_source_support_requires_same_event_and_real_quote(engine):
    a,b,e,r=fixtures()
    good=engine.validate_crosscheck(a,e,[b],r)
    assert good['passed'] and good['has_primary'] and good['independent_reporting_count']==1
    assert not good.get('independent_verification',False)
    bad=copy.deepcopy(r);bad['sources'][0]['comparisons'][0]['quote']='a quote that does not exist in the article'
    with pytest.raises(ValueError,match='原文连续片段'):
        engine.validate_crosscheck(a,e,[b],bad)
    # 同事件不成立或置信不足时不能算旁证：报告不会变成 crosschecked，也不把对方机构算成支持方。
    bad=copy.deepcopy(r);bad['sources'][0]['same_event']=False
    report=engine.validate_crosscheck(a,e,[b],bad)
    assert report['state']=='primary_announcement' and not report['sources'][0]['counted_as_corroboration']
    bad=copy.deepcopy(r);bad['sources'][0]['confidence']=.5
    report=engine.validate_crosscheck(a,e,[b],bad)
    assert report['state']=='primary_announcement' and not report['sources'][0]['counted_as_corroboration']


def test_conflicting_qualified_fact_blocks_automatic_draft(engine):
    a,b,e,r=fixtures();r['sources'][0]['comparisons'][0]['judgment']='contradicts'
    report=engine.validate_crosscheck(a,e,[b],r)
    assert not report['passed'] and report['state']=='conflict'


def test_reposts_and_same_owner_are_not_independent_corroboration(engine):
    a,b,e,r=fixtures();r['sources'][0]['relationship']='syndicated'
    report=engine.validate_crosscheck(a,e,[b],r)
    assert report['state']=='primary_announcement' and not report['sources'][0]['counted_as_corroboration']
    b=story('other','google-deepmind',b['title'],b['text'])
    r['sources'][0]['relationship']='primary_material'
    report=engine.validate_crosscheck(a,e,[b],r)
    assert not report['sources'][0]['counted_as_corroboration'] and report['supporting_organizations']==['Google']


def test_duplicate_body_overrules_model_claim_of_original_reporting(engine):
    a,b,e,r=fixtures();b['text']=a['text'];r['sources'][0]['comparisons'][0]['quote']=a['text'][:60]
    report=engine.validate_crosscheck(a,e,[b],r)
    assert report['sources'][0]['relationship']=='syndicated' and report['state']=='primary_announcement'
    assert not report['sources'][0]['counted_as_corroboration'] and near_copy(a['text'],a['text'])


def test_secondary_only_needs_two_original_reporting_owners(engine):
    a,b,e,r=fixtures();a=story('anchor','techcrunch-ai',a['title'],a['text'])
    r['anchor_relationship']='announcement_rewrite'
    assert not engine.validate_crosscheck(a,e,[b],r)['passed']
    r['anchor_relationship']='original_reporting'
    report=engine.validate_crosscheck(a,e,[b],r)
    assert report['passed'] and not report['has_primary'] and report['independent_reporting_count']==2


def test_candidate_recall_is_not_evidence_and_excludes_old(engine):
    a,b,e,r=fixtures();engine.store.put('stories',a);engine.store.put('stories',b)
    unrelated=story('unrelated','techcrunch-ai','Google opens Singapore office','Unrelated office news')
    engine.store.put('stories',unrelated)
    assert [s['id'] for s in engine.related_stories(a)]==['other']
    b['published_at']=(datetime.now(timezone.utc)-timedelta(days=5)).isoformat();engine.store.put('stories',b)
    assert engine.related_stories(a)==[]


def test_missing_corroboration_keeps_media_story_without_writing(engine,monkeypatch):
    # 媒体二手（小道消息）没有另一家机构的同事件报道时仍然拦下，不写稿。
    a,b,e,r=fixtures();a=story('anchor','techcrunch-ai',a['title'],a['text'])
    engine.store.put('stories',a)
    monkeypatch.setattr(engine,'related_stories',lambda _ : [])
    with pytest.raises(CorroborationPending):
        engine.crosscheck_story(a,e,engine.new_run('test'))
    assert not engine.store.all('calls') and not engine.store.all('drafts')
    assert engine.store.get('stories','anchor')['crosscheck']['state']=='awaiting_corroboration'


def test_official_exception_can_be_disabled_by_policy(engine,monkeypatch):
    from newsroom.source_quality import policy
    rules=dict(policy());rules['allow_primary_announcement_only']=False
    monkeypatch.setattr('newsroom.crosscheck.policy',lambda: rules)
    a,b,e,r=fixtures()
    report=engine.validate_crosscheck(a,e,[],{'anchor_relationship':'primary_material','sources':[],'summary':'没有对照'})
    assert not report['passed'] and report['state']=='awaiting_corroboration'


def test_official_announcement_without_coverage_passes_with_explicit_label(engine,monkeypatch):
    # 官方一手公告允许没有第二家机构报道，但报告必须标明"仅官方公告"，且媒体不享受该例外。
    a,b,e,r=fixtures()
    engine.store.put('stories',a)
    monkeypatch.setattr(engine,'related_stories',lambda _ : [])
    report=engine.crosscheck_story(a,e,engine.new_run('test'))
    assert report['state']=='primary_announcement' and report['passed']
    assert report['primary_announcement_only'] is True
    assert '机构自述' in report['summary'] and any('官方一手公告' in w for w in report['warnings'])
    media=story('anchor','techcrunch-ai',a['title'],a['text'])
    engine.store.put('stories',media)
    with pytest.raises(CorroborationPending):
        engine.crosscheck_story(media,e,engine.new_run('test'))


def test_date_only_official_page_is_eligible_with_day_precision_label():
    # 官方列表只有日期时允许成稿，但必须带"按天精度"标注；媒体二手仍要求精确发布时间。
    source=next(s for s in sources() if s['id']=='anthropic-news')
    day=datetime.now(timezone.utc).strftime('%b %d, %Y')
    feed=parse_news_html(f'<a href="/news/test"><h3>New Claude</h3><time>{day}</time></a>',source)
    assert feed.entries[0]['date_precision']=='day'
    item={'url':'https://www.anthropic.com/news/test','published_at':now(),'date_basis':'published_date_only'}
    official=assessment(source,item)
    assert official['auto_eligible'] and '按天精度' in official['note'] and official['max_age_hours']==72
    assert assessment(source,item,corroboration=True)['auto_eligible']
    media=next(s for s in sources() if s['id']=='techcrunch-ai')
    media_item={'url':'https://techcrunch.com/2026/10/19/test/','published_at':now(),'date_basis':'published_date_only'}
    assert not assessment(media,media_item)['auto_eligible']


def test_official_window_is_longer_than_media_window():
    from datetime import timedelta
    old=(datetime.now(timezone.utc)-timedelta(hours=48)).isoformat()
    official=next(s for s in sources() if s['id']=='anthropic-news')
    assert assessment(official,{'url':'https://www.anthropic.com/news/test','published_at':old,'date_basis':'published'})['auto_eligible']
    media=next(s for s in sources() if s['id']=='techcrunch-ai')
    assert not assessment(media,{'url':'https://techcrunch.com/2026/10/19/test/','published_at':old,'date_basis':'published'})['auto_eligible']


def test_selected_comparison_source_must_be_rechecked_before_handoff(engine,monkeypatch):
    a,b,e,r=fixtures();engine.store.put('stories',b)
    report=engine.validate_crosscheck(a,e,[b],r)
    # 只有整页哈希变化不算实质改动；必须让对照引文真的从页面消失才会拦下。
    monkeypatch.setattr(engine,'prepare_source',lambda item,**kw:{**item,'text':'页面被替换成无关内容','source_snapshot':{'body_hash':'changed','body_length':12}})
    with pytest.raises(ValueError,match='对照信源原文已变化'):
        engine.verify_crosscheck({'demo':False,'crosscheck':report})


def test_real_mode_pipeline_comparison_prompt_writer_review_handoff(engine, monkeypatch):
    from newsroom.collect import demo_items
    from newsroom.llm import ModelClient
    from newsroom.source_quality import snapshot
    # 外部网络和模型均替身；运行完整真实模式门禁，不产生付费或发表动作。
    a,b,e,r=fixtures()
    body=demo_items()[0]['text']
    a['text']=body
    b['text']='We independently reviewed the public document assistant announcement. '+body+' Our coverage distinguishes launch facts from any untested product performance.'
    r['sources'][0]['comparisons'][0]['quote']='该项目提供本地文档检索和回答出处显示。'
    s=engine.settings;s.demo=False;s.llm_model='writer';s.llm_api_key='fake';s.llm_base_url='https://writer.example';s.prompt_model='planner'
    engine.store.put('stories',a);engine.store.put('stories',b)
    monkeypatch.setattr('newsroom.collect.collect',lambda *args,**kwargs:[])
    def prepare(item,corroboration=False):
        src=engine.source_for(item)
        item['source_snapshot']=snapshot(src,item,'<article>'+item['text']+'</article>',item['text'],corroboration=corroboration)
        engine.store.put('stories',item)
        return item
    monkeypatch.setattr(engine,'prepare_source',prepare)
    seen=[]
    def model(stage,context,run,**kwargs):
        seen.append(stage)
        if stage=='crosscheck':return r
        if stage=='review':assert context['crosscheck']['passed']
        return ModelClient.demo(stage,context)
    monkeypatch.setattr(engine.model,'complete',model)
    engine.enqueue('pipeline',payload={'story_id':'anchor','editor':__import__('newsroom.editors',fromlist=['choose_editor']).choose_editor(engine.store)})
    engine.work_once()
    d=engine.store.all('drafts')[0]
    assert d['status']=='pending_human' and d['crosscheck']['state']=='crosschecked'
    assert seen.index('crosscheck')<seen.index('prompt')<seen.index('write')<seen.index('review')
    assert (s.data_dir/'exports'/d['id']/'v1/crosscheck.json').is_file()
