// Controls and usage are server-persisted. Only notification dismissal is local to this tab.
Object.assign(labels,{paused:'已暂停',cancelled:'已中断'});
const controlPanel=document.createElement('div');
controlPanel.className='panel';
controlPanel.innerHTML='<h2>运行控制与用量</h2><p id="workflow-status" role="status" aria-live="polite"></p>'+
  '<div class="decision-row"><button id="workflow-pause" class="secondary">暂停采编</button><button id="workflow-resume" class="primary">继续采编</button><button id="workflow-cancel" class="secondary">中断本轮后续步骤</button></div>'+
  '<p class="small">暂停保留进度，继续时接着执行。已发出的请求会先收完并保存，仍可能计费；中断后保留成稿，定时和排队采编保持暂停。重启后不能恢复原运行栈，可用已有稿件继续处理。微信提交结果请在发布记录中核对。</p>'+
  '<p id="usage-summary" role="status" aria-live="polite"></p><p id="run-usage"></p>'+
  '<form id="usage-form"><label for="usage-threshold">每轮 token 提醒值（0 关闭，仅提醒、不限制出稿）</label><div class="decision-row"><input id="usage-threshold" type="number" min="0" max="1000000000" step="1000" value="50000"><button class="secondary" type="submit">保存提醒值</button></div></form>'+
  '<p class="small">仅累计服务商返回的用量；进行中及未返回 usage 的请求无法准确计数，失败请求也可能收费。实际账单以服务商为准。交稿、复制和提交草稿不额外调用模型。</p>';
$('desk').prepend(controlPanel);
let usageDirty=false,controlBusy=false;
const usageNotified=new Set();
$('usage-threshold').oninput=()=>{usageDirty=true;};
$('usage-form').onsubmit=event=>{event.preventDefault();action(async()=>{
  const threshold=Number($('usage-threshold').value);
  if(!Number.isInteger(threshold)||threshold<0||threshold>1000000000)throw new Error('请输入有效的非负整数');
  await api('/usage/preferences','PUT',{run_alert_tokens:threshold});usageDirty=false;toast('用量提醒已保存，不会限制出稿');
});};
async function workflowAction(command,jobId){
  if(controlBusy)return;
  controlBusy=true;
  try{
    await api('/workflow/control','POST',{action:command,...(jobId?{job_id:jobId}:{})});
    toast({pause:'已请求暂停；当前请求收完后停止后续步骤',resume:'继续原进度，恢复采编调度',cancel:'已请求中断后续步骤；已发请求仍可能计费，采编调度保持暂停'}[command]);
    await refresh();
  }catch(error){toast(error.message);}finally{controlBusy=false;}
}
$('workflow-pause').onclick=()=>workflowAction('pause');
$('workflow-resume').onclick=()=>workflowAction('resume');
$('workflow-cancel').onclick=()=>workflowAction('cancel',state?.workflow_control?.job_id||state?.jobs.find(j=>['running','paused'].includes(j.status)&&['pipeline','collect','review','draft_batch','images'].includes(j.kind))?.id);

function renderControls(){
  const control=state.workflow_control||{},usage=state.usage||{};
  const job=state.jobs.find(j=>['running','paused'].includes(j.status)&&['pipeline','collect','review','draft_batch','images'].includes(j.kind));
  const run=job&&state.runs.find(r=>r.id===job.run_id);
  const pending=control.requested==='pause'&&job?.status!=='paused';
  $('workflow-status').textContent=control.requested==='cancel'?'正在中断后续步骤，等待当前请求收完并保存…':pending?'已请求暂停，等待当前请求收完并保存…':job?.status==='paused'?'已暂停在控制点，点击继续将接着当前进度执行。':control.dispatch_paused?'采编调度已暂停；排队和定时采编不会启动。':job?'采编运行中，可随时暂停或中断后续步骤。':'采编调度正常。';
  $('workflow-pause').disabled=Boolean(control.dispatch_paused);
  $('workflow-resume').disabled=!control.dispatch_paused||control.requested==='cancel';
  $('workflow-cancel').disabled=!job||control.requested==='cancel';
  if(control.dispatch_paused){$('run').disabled=true;$('run').textContent='采编已暂停，请先点击继续';}
  if(run?.status==='paused'){
    $('active-run').hidden=false;$('active-message').textContent='已暂停 · '+(run.message||'保留当前进度');
    $('active-stage').textContent='不会开始下一次请求；继续后接着执行';$('progress').value=run.progress||5;
  }
  $('usage-summary').textContent='今日（北京时间）已报告 '+Number(usage.reported_tokens||0).toLocaleString()+' tokens · '+(usage.requests||0)+' 次请求'+(usage.unknown_usage_requests?' · '+usage.unknown_usage_requests+' 次用量未知':'');
  $('run-usage').textContent=run?'本轮已报告 '+Number(run.token_usage?.total_tokens||0).toLocaleString()+' tokens · '+(run.calls||0)+' 次请求'+(run.active_model_call?' · 当前请求用量待返回':'')+(run.unknown_usage_requests?' · '+run.unknown_usage_requests+' 次用量未知':''):'';
  if(!usageDirty)$('usage-threshold').value=usage.preferences?.run_alert_tokens??50000;
  for(const item of state.runs){
    if(item.token_alert&&!usageNotified.has(item.id)){
      usageNotified.add(item.id);toast('用量提醒：本轮已报告 '+Number(item.token_usage?.total_tokens||0).toLocaleString()+' tokens，可暂停或中断后续步骤');
    }
  }
}

async function sendToWechatDraft(draft,button){
  if(dirty&&selected?.id===draft.id){toast('请先保存正文修改，再提交当前版本');return;}
  button.disabled=true;
  try{
    const record=await api('/drafts/'+draft.id+'/wechat-draft','POST',{revision:draft.revision,confirmed:true});
    toast(record.status==='draft_only'?'此版本已在微信草稿箱，无需重复提交':record.status==='unknown'?'提交结果待核对，请查看发布记录':'已排队保存到微信草稿箱；完成状态见发布记录');
    await refresh();
  }catch(error){toast(error.message);}finally{if(button.isConnected)button.disabled=false;}
}
function draftButton(draft){
  if(draft.demo||!['pending_human','ready_to_publish'].includes(draft.status))return null;
  const current=state.publications.find(p=>p.draft_id===draft.id&&p.revision===draft.revision&&p.account_current&&p.status!=='cancelled');
  const button=document.createElement('button');button.className='primary one-click-draft';button.type='button';
  button.textContent=current?.status==='draft_only'?'已存入微信草稿箱':current&&['prepared','draft_created'].includes(current.status)?'草稿处理中…':current?.status==='unknown'?'提交结果待核对':'一键提交微信草稿';
  button.disabled=!state.publication?.ready||Boolean(current&&['draft_only','prepared','draft_created','unknown','submitting','accepted','publishing','published'].includes(current.status));
  if(!state.publication?.config?.enabled)button.title='先在发布设置中配置封面与署名并启用当前账号';
  button.onclick=()=>sendToWechatDraft(draft,button);return button;
}
const deskBeforeControls=renderDesk;
renderDesk=function(){
  deskBeforeControls();renderControls();
  document.querySelectorAll('#draft-list .draft-card').forEach(card=>{
    const draft=state.drafts.find(d=>d.id===card.querySelector('.open-draft')?.dataset.id);
    if(draft){const button=draftButton(draft);if(button)card.append(button);}
  });
  if(selected&&$('draft-dialog').open){renderDraftPublish();const old=$('draft-one-click');if(old)old.remove();const draft=state.drafts.find(d=>d.id===selected.id);if(draft){const button=draftButton(draft);if(button){button.id='draft-one-click';$('publish-block').prepend(button);}}}
};
const showDraftBeforeControls=showDraft;
showDraft=async function(id,reset=true){
  await showDraftBeforeControls(id,reset);
  if(state.settings.review_mode==='off')$('rereview').textContent='完成程序检查（不调用模型）';
  const old=$('draft-one-click');if(old)old.remove();
  const button=draftButton(selected);if(button){button.id='draft-one-click';$('publish-block').prepend(button);}
};
const activityBeforeControls=renderActivity;
renderActivity=function(){
  activityBeforeControls();
  document.querySelectorAll('#run-list .run-entry').forEach((card,index)=>{
    const run=state.runs[index];if(run?.unknown_usage_requests){const p=document.createElement('p');p.className='small';p.textContent=run.unknown_usage_requests+' 次请求用量未知，未计入已报告 tokens';card.append(p);}
  });
  const queued=state.jobs.filter(j=>j.status==='queued'&&['pipeline','collect','review','draft_batch','images'].includes(j.kind));
  for(const job of queued){const p=document.createElement('p');p.textContent='排队任务 · '+job.kind+' · '+time(job.created_at)+' ';const b=document.createElement('button');b.className='secondary';b.textContent='取消排队';b.onclick=()=>workflowAction('cancel',job.id);p.append(b);$('run-list').prepend(p);}
};
