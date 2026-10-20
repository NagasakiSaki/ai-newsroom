labels.awaiting_corroboration='等待多源旁证';labels.conflict='多源事实冲突';labels.crosschecked='已完成多源对照';labels.primary_announcement='仅官方公告（无独立报道）';labels.discovery_only='仅供选题线索';stages.crosscheck='多源事实对照';labels.needs_source_review='信源待复核';stages.prompt='策划写作提示词';
stages.plan='责任编辑分工';stages.visual='插图与排版';
let studioLoaded=false,rewriteTarget=null;
const originalNavigate=navigate;
navigate=function(tab){originalNavigate(tab);if(tab==='compose')$('page-title').textContent='撰稿工作室';};
const studioFormatLabel=document.createElement('label');studioFormatLabel.htmlFor='studio-format';studioFormatLabel.textContent='体裁';
const studioFormat=document.createElement('select');studioFormat.id='studio-format';
studioFormat.innerHTML='<option value="news">新闻解读（事实为主）</option><option value="commentary">评论（观点与判断）</option>';
$('studio-custom').before(studioFormat);$('studio-custom').before(studioFormatLabel);
function fillEditor(){const e=state.editors.find(x=>x.id===$('studio-editor').value);if(!e)return;for(const k of ['name','style_name','instructions','prompt_profile','writer_profile','prompt_model','writer_model'])$('ed-'+k).value=e[k]||'';$('ed-id').value=e.id;}
function renderStudio(){
  const auto=state.settings.editorial_mode==='auto';
  if(auto){const steps=['采集线索',state.settings.fast_mode?'批量选题':'价值评分','提炼原文证据',state.settings.fast_mode?'必要对照':'多源对照','整篇策划','一次成稿'];if(state.settings.review_mode!=='off')steps.push(state.settings.review_mode==='strict'?'AI复审':'AI建议');steps.push('自动图文','人工审核发表');document.querySelector('.steps').innerHTML=steps.map(s=>'<span>'+s+'</span>').join('<i>→</i>');}
  stages.review=state.settings.review_mode==='off'?'程序检查与交稿':'AI复审与交稿';
  labels.needs_ai_review=state.settings.review_mode==='off'?'待程序检查':'待AI复审';
  for(const step of document.querySelectorAll('.steps span'))if(['AI 审核与修订','程序检查（无AI复审）'].includes(step.textContent))step.textContent=state.settings.review_mode==='off'?'程序检查（无AI复审）':'AI 审核与修订';
  $('studio-editor').hidden=auto;document.querySelector('label[for="studio-editor"]').hidden=auto;
  const panels=document.querySelectorAll('#compose .two-columns > .panel');
  panels[0].parentElement.style.gridTemplateColumns=auto?'1fr':'';
  panels[0].querySelector('h2').textContent=auto?'选择新闻，整篇自主策划':'给每种文风配一位小编';
  panels[1].hidden=auto;
  panels[0].querySelector('.quality-note').textContent=auto?'自动选择标题、篇幅和叙事结构，写作模型一次完成整篇，不设固定分工或新闻评论比例。快速模式省略独立复审与图文模型调用，原图由程序排版。':'证据提炼 → 小编提示词 → 写作 → 审核交稿';

  const selectedStory=$('studio-story').value;
  $('studio-story').innerHTML=state.stories.filter(s=>s.demo===state.settings.demo&&(s.demo||s.source_report?.auto_eligible)).sort((a,b)=>{const rank=s=>({primary:3,secondary:2,research:1}[s.source_report?.tier]||0)*10+(['new','shortlisted','awaiting_corroboration'].includes(s.status)?3:0);return rank(b)-rank(a)||b.published_at.localeCompare(a.published_at);}).map(s=>`<option value="${esc(s.id)}">${esc(s.title)}</option>`).join('');
  if([...$('studio-story').options].some(o=>o.value===selectedStory))$('studio-story').value=selectedStory;
  if(!studioLoaded){$('studio-editor').innerHTML=state.editors.map(e=>`<option value="${esc(e.id)}">${esc(e.name)} · ${esc(e.style_name)}</option>`).join('');$('studio-editor').value=state.writing_default.editor_id;$('studio-custom').value=state.writing_default.custom_style||'';$('studio-format').value=state.writing_default.format||'news';
    for(const role of ['prompt','writer'])$('ed-'+role+'_profile').innerHTML=state.model_profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · ${p.configured?'已配置':'待配置'}</option>`).join('');fillEditor();studioLoaded=true;}
  $('studio-status').textContent=`定时采编默认：${auto?'自主策划，整篇连贯写作':state.editors.find(e=>e.id===state.writing_default.editor_id)?.name||'待设置'} · ${state.settings.demo?'当前为演示调用，不会产生真实模型费用':'提示词策划与正文写作分别请求对应模型'}`;
}
const oldSources=renderSources;renderSources=function(){oldSources();$('source-list').innerHTML=state.sources.map(s=>{const h=state.source_health.find(x=>x.id===s.id);return `<div class="source"><strong>${esc(s.name)}</strong> <span class="badge ${s.tier==='primary'?'green':'orange'}">${s.kind==='benchmark'?'独立 / 社区实测':{primary:'一手公告',research:'研究论文',secondary:'编辑部报道',discovery:'社区线索'}[s.tier]||esc(s.tier)}</span><p>${esc(s.evidence_note||s.notes||'')}</p><p><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">核对原始订阅 / 列表 ↗</a></p><p>连接：${h?h.status==='ok'?'正常':'失败':'尚未检查'} · 连续失败 ${h?.consecutive_errors||0} 次<br>最近检查：${time(h?.last_checked_at)}<br>最近成功：${time(h?.last_success_at)}<br>源内最新${s.format==='news_html'?'日期（精度天）':'发布时间'}：${s.format==='news_html'?(h?.latest_published_at?.slice(0,10)||'—'):time(h?.latest_published_at)}<br>72 小时线索窗 ${h?.recent_count??'—'} 条（含仅供线索 ${h?.discovery_only??'—'} 条）${h?.truncated?'<br>订阅条目超过本轮上限，当前结果不代表全量覆盖':''}</p>${h?.status==='error'?`<p class="error">${esc(h.error)}</p>`:''}</div>`;}).join('');$('strategy').insertAdjacentHTML('afterbegin',`<div class="quality-note">每 ${state.collection_interval_minutes} 分钟采集；${esc(scheduleDescription(state.schedule))}。<br>官方一手 ${state.source_policy.official_max_age_hours||state.source_policy.news_max_age_hours} 小时、媒体 ${state.source_policy.news_max_age_hours} 小时、论文 ${state.source_policy.paper_max_age_hours} 小时；官方列表只有日期时按天精度并标注。优先顺序：同事件其他机构 → 官方/权威 → 媒体 → 论文；论文候选按信源限额召回。官方一手公告可无第二家报道成稿，核查记录保留在审核台；宽松模式允许归因采用已登记媒体的原文，不把报道等同独立验证；事实冲突仍暂停。交稿前重读采用的原文，只有引用摘录消失或页面被整体替换才阻断。</div>`);};
const oldShowDraft=showDraft;showDraft=async function(id,reset=true){await oldShowDraft(id,reset);const d=selected;if(!d)return;$('draft-details').insertAdjacentHTML('afterbegin',`<div class="review-block"><h3>${esc(d.editor?.name||'历史小编')} · ${esc(d.editor?.style_name||'历史文风')}</h3><p>提示词模型：${esc(d.models?.prompt?.model||d.editor?.prompt_model||'历史记录未保存')}<br>写作模型：${esc(d.models?.writer?.model||d.editor?.writer_model||'历史记录未保存')}</p><button id="rewrite-studio" class="secondary">选择文风重新撰稿</button><details><summary>查看本稿写作提示词</summary><pre class="prompt-view">${esc(JSON.stringify(d.writing_prompt||{},null,2))}</pre></details><details><summary>信源检查报告</summary><pre class="prompt-view">${esc(JSON.stringify(d.source_snapshot||{},null,2))}</pre></details></div>`);$('rewrite-studio').disabled=['manual_published','published','submitted','publish_unknown'].includes(d.status);$('rewrite-studio').onclick=()=>{rewriteTarget={draft_id:d.id,revision:d.revision,story_id:d.story_id};$('draft-dialog').close();navigate('compose');$('studio-story').value=d.story_id;$('studio-target').textContent='重写当前稿件 v'+d.revision+'；生成后须重新审核';};};
$('studio-editor').onchange=fillEditor;
$('studio-new').onclick=()=>{document.querySelector('.editor-advanced').open=true;$('ed-id').value='editor-'+Date.now().toString(36);$('ed-name').value='新小编';$('ed-style_name').value='自定义文风';$('ed-instructions').value='用清晰、准确的中文解释 AI 新闻，交代证据、影响与局限。';};
async function saveEditor(){const custom=$('studio-custom').value;const body={};for(const k of ['id','name','style_name','instructions','prompt_profile','writer_profile','prompt_model','writer_model'])body[k]=$('ed-'+k).value.trim();const saved=await api('/editors/'+encodeURIComponent(body.id),'PUT',body);state.editors=[...state.editors.filter(e=>e.id!==saved.id),saved];studioLoaded=false;renderStudio();$('studio-editor').value=body.id;$('studio-custom').value=custom;fillEditor();studioLoaded=true;return body.id;}
$('studio-save').onclick=()=>action(async()=>{if(state.settings.editorial_mode!=='auto')await saveEditor();toast('小编文风与两种模型绑定已保存');});
function writingChoice(){return {editor_id:state.settings.editorial_mode==='auto'?null:$('studio-editor').value,custom_style:$('studio-custom').value,format:$('studio-format').value};}
$('studio-default').onclick=()=>action(async()=>{if(state.settings.editorial_mode!=='auto')await saveEditor();await api('/writing-default','PUT',writingChoice());toast('已保存一键运行与定时采编的写作偏好');});
$('studio-compose').onclick=()=>action(async()=>{if(state.settings.editorial_mode!=='auto')await saveEditor();const story_id=$('studio-story').value;if(!story_id)throw Error('暂无通过信源检查的新闻；可先运行采集');if(rewriteTarget&&rewriteTarget.story_id!==story_id)rewriteTarget=null;await api('/compose','POST',{...writingChoice(),story_id,...(rewriteTarget||{})});rewriteTarget=null;$('studio-target').textContent='新建稿件';toast('已排队：提炼证据 → 策划整篇结构 → 一次写成 → 自动图文交稿');navigate('desk');});
$('studio-reset').onclick=()=>{rewriteTarget=null;$('studio-target').textContent='新建稿件';};
$('check-sources').onclick=()=>action(async()=>{await api('/sources/check','POST');toast('已排队检查真实 RSS 信源，不调用大模型');});

const crossJudgment={supports:'一致',contradicts:'冲突',different_scope:'口径 / 范围不同',not_mentioned:'未提及'};
const sourceRelation={primary_material:'一手材料',original_reporting:'原创报道（模型判断）',announcement_rewrite:'公告改写',syndicated:'转载',unknown:'出处关系未确定'};
function crosscheckHTML(c){if(!c)return '<p class="muted">历史稿件尚无多源对照报告，重新撰稿后生成。</p>';return `<p>${badge(c.state)}</p><p>${esc(c.summary)}</p><p class="small">采用的机构：${esc((c.supporting_organizations||[]).join('、')||'演示')} · 原创二手报道 ${c.independent_reporting_count??0} 篇</p>${(c.sources||[]).map(r=>`<details class="cross-source"><summary>${esc(r.source)} · ${r.same_event?'同事件 '+Math.round(r.confidence*100)+'%':'非同一事件'}</summary><p>${esc(sourceRelation[r.relationship]||r.relationship)} · ${esc(r.reason)}</p><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">核对对照原文 ↗</a>${r.comparisons.map(f=>`<div class="comparison"><strong>[${esc(f.fact_id)}] ${esc(crossJudgment[f.judgment])}</strong><p>${esc(f.explanation)}</p>${f.quote?`<blockquote>${esc(f.quote)}</blockquote>`:''}</div>`).join('')}</details>`).join('')}<p class="muted">${(c.warnings||[]).map(esc).join('；')}</p>`;}
const showDraftWithEditor=showDraft;
showDraft=async function(id,reset=true){await showDraftWithEditor(id,reset);if(selected)$('draft-details').insertAdjacentHTML('afterbegin','<div class="review-block"><h3>多信源事实对照</h3>'+crosscheckHTML(selected.crosscheck)+'</div>');};
const storiesBeforeCross=renderStories;
renderStories=function(){const expanded=new Set([...document.querySelectorAll('#story-list details[data-group][open]')].map(e=>e.dataset.group));storiesBeforeCross();const byId=new Map(state.stories.map(s=>[s.id,s]));const groups=(state.event_candidates||[]).filter(g=>g.related_ids.length&&byId.has(g.id)).sort((a,b)=>{const x=byId.get(a.id),y=byId.get(b.id);return Number(y.source_report?.tier!=='research')-Number(x.source_report?.tier!=='research')||y.published_at.localeCompare(x.published_at);}).slice(0,16);$('story-list').insertAdjacentHTML('afterbegin',`<div class="event-groups"><h3>同事件候选组</h3><p class="muted">先按主题与时间召回；候选组不代表事实已核实。撰稿时读取双方原文，才做逐条事实对照。</p>${groups.map(g=>{const anchor=state.stories.find(s=>s.id===g.id);if(!anchor)return '';return `<details data-group="${esc(g.id)}" ${expanded.has(g.id)?'open':''}><summary><span class="badge">${esc(anchor.source)}</span> ${esc(anchor.title)} · ${g.related_ids.length} 条对照候选</summary><p>${anchor.crosscheck?esc(anchor.crosscheck.summary):'尚未进行模型事实对照'}</p>${g.related_ids.map(id=>state.stories.find(s=>s.id===id)).filter(Boolean).map(s=>`<p><span class="badge">${esc(s.source)}</span> <a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></p>`).join('')}${anchor.crosscheck?crosscheckHTML(anchor.crosscheck):''}</details>`;}).join('')||'<p class="muted">暂未召回相关报道，等待下轮采集。</p>'}</div>`);};

const showDraftBeforeTeam=showDraft;
showDraft=async function(id,reset=true){
  await showDraftBeforeTeam(id,reset);
  const draft=state.drafts.find(d=>d.id===id);if(!draft)return;
  if(draft.editorial_plan){
    const panel=document.createElement('div');panel.className='review-block';
    const title=document.createElement('h3');title.textContent=draft.editorial_plan.mode==='single_article'?'整篇自主策划':draft.editorial_plan.mode==='autonomous'?'历史三段分工':'责任编辑与接力小编';panel.append(title);
    const reason=document.createElement('p');reason.textContent=draft.editorial_plan.reason+(draft.editorial_plan.mode==='autonomous'?' · 新闻占比目标70%，本稿约'+Math.round(draft.editorial_plan.news_ratio_actual*100)+'%':'');panel.append(reason);
    for(const section of draft.editorial_plan.sections||[]){const p=document.createElement('p');p.textContent=(section.role_name||state.editors.find(e=>e.id===section.editor_id)?.name||section.editor_id)+' · '+section.heading+'：'+section.focus;panel.append(p);}
    if(draft.editorial_plan.mode==='single_article'){const info=document.createElement('p');info.textContent=({breaking:'突发报道',explainer:'深度解读',brief:'简洁快讯'}[draft.editorial_plan.story_mode]||'科技特写')+' · 策划目标约'+draft.editorial_plan.target_chars+'字 · 实际'+draft.editorial_plan.actual_chars+'字';panel.append(info);for(const beat of draft.editorial_plan.narrative_beats||[]){const p=document.createElement('p');p.textContent=beat;panel.append(p);}}
    $('draft-details').prepend(panel);
  }
  if(draft.visuals){
    const panel=document.createElement('div');panel.className='review-block';
    const title=document.createElement('h3');title.textContent='图文与排版';panel.append(title);
    const originals=(draft.visuals.assets||[]).filter(a=>a.kind==='official_image'||a.kind==='benchmark_image');
    const info=document.createElement('p');info.textContent=originals.length+' 张新闻原图＋'+((draft.visuals.assets||[]).length-originals.length)+' 张原创信息图 · '+(draft.visuals.state==='fallback'?'使用兜底方案':draft.visuals.state==='automatic_layout'?'程序自动排版（未调用图文模型）':'图文模型规划');panel.append(info);
    if(draft.visuals.error){const error=document.createElement('p');error.textContent=draft.visuals.error;panel.append(error);}
    for(const image of originals){
      const preview=document.createElement('img');preview.src='/media/'+image.file;preview.alt=image.title;preview.loading='lazy';preview.style.cssText='width:100%;max-height:220px;object-fit:contain';panel.append(preview);
      const p=document.createElement('p');const a=document.createElement('a');a.href=image.original_url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=image.credit+' · 查看原图';p.append(a);panel.append(p);
    }
    for(const error of draft.visuals.source_images?.errors||[]){const p=document.createElement('p');p.className='muted';p.textContent='原图 '+error.image_id+'：'+error.error;panel.append(p);}
    if(!originals.length&&(draft.visuals.source_images?.available_count||0)>0){const p=document.createElement('p');p.textContent='发现了新闻原图，但本次未能成功下载；详情见上方原因。';panel.append(p);}
    if(draft.editorial_plan.mode==='single_article'){const info=document.createElement('p');info.textContent=({breaking:'突发报道',explainer:'深度解读',brief:'简洁快讯'}[draft.editorial_plan.story_mode]||'科技特写')+' · 策划目标约'+draft.editorial_plan.target_chars+'字 · 实际'+draft.editorial_plan.actual_chars+'字';panel.append(info);for(const beat of draft.editorial_plan.narrative_beats||[]){const p=document.createElement('p');p.textContent=beat;panel.append(p);}}
    $('draft-details').prepend(panel);
  }
};
