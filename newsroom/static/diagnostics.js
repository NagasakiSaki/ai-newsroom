stages.plan_article='整篇结构与提示词';stages.score_batch='批量选题评分';
labels.media_report='媒体报道（单源归因）';stages.plan_auto='自主传播策划';stages.review_skipped='程序检查';stages.collect_cache='复用近期采集';labels.awaiting_corroboration='等待同事件旁证';labels.queued='等待执行';
Object.assign(stages,{crosscheck:'多源事实对照',crosscheck_hold:'等待同事件旁证',prompt:'策划写作提示词'});
const callOutcomes={ok:'请求与输出结构通过',validation_failed:'证据校验不合格',invalid_or_rejected:'输出结构不合格',retryable_error:'请求或响应异常',http_error:'接口拒绝请求'};
const callReasons={comparison_quote_mismatch:'对照引文未逐字匹配原文',evidence_quote_or_url_mismatch:'主证据引文或网址不匹配',unknown_or_duplicate_source:'出现未知或重复信源',unknown_fact_id:'出现未知事实编号',duplicate_fact_id:'事实编号重复',schema_validation:'JSON 字段校验失败',network_error:'网络连接或空闲超时',total_timeout:'响应总耗时超限',response_too_large:'响应体超过大小限制',http_retryable:'接口暂时异常',http_error:'接口拒绝请求',invalid_response:'响应格式、截断或拒绝问题'};
callOutcomes.cache_hit='复用已验证阶段（无新增API费用）';callOutcomes.continuation='服务商输出截断，接续长稿';
Object.assign(callReasons,{visual_unknown_reference:'插图引用了未知事实或图片',editorial_plan_order:'接力顺序不合格或小编重复'});
function triggerLabel(value){return{schedule:'定时采编',collection_schedule:'定时采集',manual:'手动触发'}[value]||'触发方式未记录';}
function runMessage(run){if(run.status==='interrupted')return '运行已中断，未自动重放。最后记录：'+(run.message||'无阶段消息');if(run.finished_at&&run.kind==='collection'&&(run.message||'').startsWith('正在'))return '采集已结束：'+(run.collected??0)+' 条时效内线索；'+(run.source_failures??0)+' 个来源失败';if(run.generated===0&&(run.message||'').includes('等待 AI / 人工审核'))return run.failures?'未生成稿件：采编失败，请查看该轮错误事件':'未生成稿件：请查看该轮候选不足或旁证拦截事件；没有新稿等待审核';return run.message||'';}
renderActivity=function(){
  const expanded=new Set(Array.from(document.querySelectorAll('#run-list details[open]'),detail=>detail.dataset.run));
  $('run-list').innerHTML=state.runs.map(run=>{
    const calls=state.calls.filter(call=>call.run_id===run.id);
    const total=run.token_usage?.total_tokens??calls.reduce((sum,call)=>sum+(Number(call.usage?.total_tokens)||0),0);
    const job=state.jobs.find(job=>job.run_id===run.id||job.id===run.job_id);
    const active=run.active_model_call;
    return `<div class="run-entry">${badge(run.status)} <span class="small">${time(run.started_at)} · ${triggerLabel(run.trigger)}</span>
      <p>${run.kind==='pipeline'?'完整采编':run.kind==='collection'?'RSS 信源采集（不调用模型）':run.kind==='draft_batch'?'批量建立微信草稿':run.kind==='writing_preview'?'整篇写作预览（不发布）':'稿件复审'} · ${run.demo?'演示调用 '+calls.length:run.calls+' 次请求'} · 已记录 ${esc(total)} tokens${run.generated!==undefined?' · 生成 '+esc(run.generated)+' 篇':''}</p>
      <p>${esc(runMessage(run))}${run.awaiting_corroboration?' · '+esc(run.awaiting_corroboration)+' 条待旁证':''}</p>
      ${active?`<p>正在请求：${esc(stages[active.stage]||active.stage)} · 第 ${esc(active.attempt)} 次 · 已等待 ${Math.max(0,Math.round((Date.now()-Date.parse(active.started_at))/1000))} 秒</p>`:''}
      <p class="small">运行 ${esc(run.id)}${job?' · 任务 '+esc(labels[job.status]||job.status)+'；业务结果以本条运行状态为准':''}</p>
      ${calls.length?`<details data-run="${esc(run.id)}" ${expanded.has(run.id)?'open':''}><summary>最近保留的请求明细（${calls.length} 条）</summary>${calls.slice().reverse().map(call=>{
        const diagnostic=call.diagnostic;
        const detail=diagnostic?(callReasons[diagnostic.code]||diagnostic.code)+(diagnostic.http_status?' · HTTP '+diagnostic.http_status:'')+(diagnostic.location?' · '+diagnostic.location:'')+(diagnostic.fields?' · '+diagnostic.fields.map(field=>field.location+': '+field.type).join('；'):''):'';
        return `<div class="call-diagnostic"><strong>${esc(stages[call.stage]||call.stage)} · ${esc(callOutcomes[call.outcome]||call.outcome)}</strong><p>${esc(call.model)}${call.attempt?' · 第 '+esc(call.attempt)+' 次':''}${call.duration_ms!==undefined?' · '+esc(call.duration_ms)+' ms':''}</p>${detail?'<p>'+esc(detail)+'</p>':''}</div>`;
      }).join('')}</details>`:''}</div>`;
  }).join('')||'<p class="muted">暂无运行记录。</p>';
  $('event-list').innerHTML=state.events.map(event=>`<div class="event"><span class="small">${time(event.at)} · ${esc(stages[event.stage]||event.stage)}</span><p>${esc(event.message)}</p></div>`).join('')||'<p class="muted">执行后会记录每个阶段。</p>';
};
const renderDeskBeforeDiagnostics=renderDesk;
renderDesk=function(){renderDeskBeforeDiagnostics();const run=state.runs.find(run=>run.status==='running');if(run){const active=run.active_model_call;$('active-stage').textContent=(stages[run.stage]||run.stage||'准备中')+' · '+triggerLabel(run.trigger)+(active?' · '+(stages[active.stage]||active.stage)+'第 '+active.attempt+' 次请求，已等待 '+Math.max(0,Math.round((Date.now()-Date.parse(active.started_at))/1000))+' 秒':'');}};

const activityBeforeFunnel=renderActivity;
renderActivity=function(){activityBeforeFunnel();document.querySelectorAll('#run-list .run-entry').forEach((card,index)=>{const f=state.runs[index]?.funnel;if(!f)return;const p=document.createElement('p');p.className='small';p.textContent='候选 '+f.candidates+' → 入选 '+f.selected+' → 原文 '+f.source_ready+' → 证据 '+f.extracted+' → 来源检查 '+f.crosschecked+' → 正文 '+f.written+' → 交稿 '+f.review_passed;card.append(p);});};
