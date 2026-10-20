// 发布设置与发布记录；固定模板，服务端文本只写入 value/textContent 或经 esc 转义。
const publishLabels={prepared:'准备中',draft_created:'草稿已建立',submitting:'正在提交发布',accepted:'发布已受理',publishing:'平台处理中',published:'已发布',failed:'明确失败',blocked:'发布前检查未通过',unknown:'结果待核对',cancelled:'已放弃',draft_only:'仅建草稿'};
const publishAttention={draft_unknown:'创建草稿的结果未确认',submit_unknown:'提交发布的结果未确认',interrupted:'程序中断，发布结果未知',query_exhausted:'自动查询次数已用尽',cover_upload:'封面上传结果未确认',publish_disabled:'发布已关闭，排队写操作已暂停',account_changed:'公众号已切换，该任务已暂停'};

const publishNav=document.createElement('button');
publishNav.className='nav';publishNav.dataset.tab='publish';publishNav.textContent='⇪　发布到公众号';
document.querySelector('nav').insertBefore(publishNav,document.querySelector('[data-tab="wechat"]'));

const publishPage=document.createElement('section');
publishPage.id='publish';publishPage.className='tab';publishPage.hidden=true;
publishPage.innerHTML=
  '<div class="two-columns">'+
    '<div class="panel">'+
      '<p class="eyebrow">WECHAT PUBLISHING</p>'+
      '<h2>微信草稿交付</h2>'+
      '<p class="muted">自动配图与选封面，完成程序检查后建立草稿。可自动交付，也可一键批量提交；结果未知时先核对，不重复创建。正式发表在公众号后台完成。</p>'+
      '<div id="publish-status" class="connection-result" role="status" aria-live="polite">正在读取发布设置…</div>'+
      '<form id="publish-form" autocomplete="off">'+
        '<label class="checkbox"><input id="pub-enabled" type="checkbox"><span>启用微信草稿交付（关闭后停止上传和建立草稿）</span></label>'+
        '<label for="pub-mode">发布方式</label>'+
        '<select id="pub-mode"><option value="manual">仅手动：人工确认后点按钮发布</option><option value="draft">仅建草稿：自动放进公众号草稿箱，人工在后台发布</option><option value="auto">全自动：审定后自动提交发布</option></select>'+
        '<label class="checkbox"><input id="pub-auto-approve" type="checkbox"><span>生成后自动建草稿：完成程序检查后存入微信草稿箱</span></label>'+
        '<label for="pub-author">发布署名（作者，≤16 字）</label><input id="pub-author" maxlength="16" placeholder="AI 编辑部">'+
        '<label for="pub-cover-id">封面永久素材 media_id</label><input id="pub-cover-id" maxlength="200" placeholder="在公众号素材库上传封面后填写 media_id">'+
        '<label for="pub-cover-path">或本地封面文件（PNG／JPEG／GIF／BMP，≤10MB）</label><input id="pub-cover-path" maxlength="1000" placeholder="D:/assets/cover.png">'+
        '<label class="checkbox"><input id="pub-comment" type="checkbox"><span>允许文章评论</span></label>'+
        '<label class="checkbox"><input id="pub-fans-comment" type="checkbox"><span>仅粉丝可评论</span></label>'+
        '<div class="config-bottom"><button type="submit" id="publish-save" class="primary">保存草稿设置</button></div>'+
        '<p class="muted">标题上限 32 字、摘要 120 字、正文少于 2 万字符、原文链接小于 1KB，都是微信草稿接口的官方限制；超限会在提交前拦下，不会静默截断。</p>'+
      '</form>'+
    '</div>'+
    '<div class="panel">'+
      '<h2>账号能力与发布记录</h2>'+
      '<p class="muted">检测只读取令牌、草稿数量和素材数量，不创建草稿、不调用正式发布接口。</p>'+
      '<button type="button" id="pub-capability" class="secondary">检测微信草稿与素材权限</button>'+
      '<div id="pub-capability-result" class="connection-result" role="status" aria-live="polite">尚未检测。</div>'+
      '<h3>最近发布记录</h3>'+
      '<div id="pub-records"><p class="muted">还没有发布记录。</p></div>'+
    '</div>'+
  '</div>';
document.querySelector('main').insertBefore(publishPage,document.querySelector('main footer'));

let publishDirty=false,publishLoaded=false,publishBusy=false;

function publishBadge(status){
  const cls=status==='published'?'green':['failed','blocked'].includes(status)?'red':['unknown','cancelled','draft_only'].includes(status)?'orange':'';
  return '<span class="badge '+cls+'">'+esc(publishLabels[status]||status)+'</span>';
}
function capabilityText(result){
  if(!result)return '未检测';
  if(result.ok===true)return '可用';
  return '不可用'+(result.errcode?'（'+result.errcode+'）':'');
}
function applyPublishSummary(summary){
  const config=summary.config||{};
  if(!publishDirty){
    $('pub-enabled').checked=Boolean(config.enabled);
    $('pub-mode').value=['auto','draft'].includes(config.mode)?config.mode:'manual';
    $('pub-auto-approve').checked=Boolean(config.auto_approve);
    $('pub-author').value=config.author||'';
    $('pub-cover-id').value=config.cover_media_id||'';
    $('pub-cover-path').value=config.cover_path||'';
    $('pub-comment').checked=Number(config.need_open_comment)===1;
    $('pub-fans-comment').checked=Number(config.only_fans_can_comment)===1;
  }
  const account=summary.account||{};
  const modeText={auto:'全自动：审定后自动提交发布',draft:'仅建草稿：人工在公众号后台发布',manual:'仅手动：人工点按钮发布'}[config.mode]||'仅手动发布';
  const approveText=config.auto_approve?' · AI 自动终审':' · 人工终审';
  const head=config.enabled
    ?'已启用 · '+modeText+approveText+' · 账号 '+(account.appid_masked||'未填写')
    :'发布未启用：人工终审后仍需到公众号后台发表';
  const problems=(summary.problems||[]).filter(Boolean);
  const tail=problems.length?'待处理：'+problems.join('；'):(summary.ready?'配置完整；每次发布前仍会校验版本、审批与来源':'');
  $('publish-status').textContent=tail?head+' · '+tail:head;
  $('publish-status').className='connection-result '+(summary.ready?'connection-success':'');
  publishLoaded=true;
}
function publishActions(record){
  const id=esc(record.id);
  let html='';
  if(['accepted','publishing','unknown','submitting'].includes(record.status))
    html+='<button class="secondary" data-pub-action="check" data-pub-id="'+id+'">立即查询状态</button>';
  if(['failed','blocked'].includes(record.status))
    html+='<button class="secondary" data-pub-action="retry" data-pub-id="'+id+'">重新提交（重新校验）</button>';
  if(record.status==='unknown'&&record.resumable)
    html+='<button class="secondary" data-pub-action="resume" data-pub-id="'+id+'">确认未发布并重新提交</button>';
  if(record.status==='draft_only')
    html+='<button class="secondary" data-pub-action="submit" data-pub-id="'+id+'">提交发布</button>';
  if(record.status!=='published')
    html+='<button class="text-button" data-pub-action="abandon" data-pub-id="'+id+'">放弃记录</button>';
  return html||'<span class="small">无需操作</span>';
}
function bindPublishActions(root){
  root.querySelectorAll('[data-pub-action]').forEach(button=>{
    button.onclick=()=>publishAction(button.dataset.pubId,button.dataset.pubAction);
  });
}
function renderPublishRecords(){
  const records=(state&&state.publications)||[];
  $('pub-records').innerHTML=records.length?records.map(record=>{
    const draft=(state.drafts||[]).find(item=>item.id===record.draft_id);
    const title=(record.snapshot&&record.snapshot.title)||(draft&&draft.article&&draft.article.title)||record.draft_id;
    return '<div class="run-entry">'+publishBadge(record.status)+' <span class="small">'+time(record.created_at)+' · v'+esc(record.revision||'')+'</span>'+
      '<p><strong>'+esc(title)+'</strong></p>'+
      '<p class="small">草稿 media_id：'+esc(record.media_id||'—')+' · 发布 publish_id：'+esc(record.publish_id||'—')+(record.article_id?' · article_id：'+esc(record.article_id):'')+'</p>'+
      (record.attention?'<p class="error">'+esc(publishAttention[record.attention]||record.attention)+'</p>':'')+
      (record.error?'<p class="error">'+esc(record.error)+'</p>':'')+
      (record.article_url?'<p><a href="'+esc(record.article_url)+'" target="_blank" rel="noopener noreferrer">打开公众号文章 ↗</a></p>':'')+
      '<div class="decision-row">'+publishActions(record)+'</div>'+
    '</div>';
  }).join(''):'<p class="muted">还没有发布记录。人工确认稿件后，在稿件详情里发布，或启用自动发布。</p>';
  bindPublishActions($('pub-records'));
}
function latestPublication(draftId){
  return ((state&&state.publications)||[]).filter(row=>row.draft_id===draftId)
    .sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')))[0];
}
async function publishAction(id,action){
  try{
    if(action==='abandon'){
      if(!confirm('放弃后该稿件可以重新修改或发布。如果发布结果未知，请先确认公众号后台没有这篇文章。确定放弃？'))return;
      await api('/publications/'+id+'/abandon','POST',{confirmed:true,note:'页面放弃'});
      toast('已放弃该发布记录，稿件可以重新处理');
    }else if(action==='resume'){
      if(!confirm('请先在公众号后台确认这篇草稿没有发表、也没有正在发布。确认后重新提交同一草稿？'))return;
      await api('/publications/'+id+'/resume','POST',{confirmed:true});
      toast('已重新提交同一草稿，不会重复创建草稿');
    }else if(action==='submit'){
      if(!confirm('确认把公众号草稿箱里的这条草稿提交发布？提交后按平台审核结果处理。'))return;
      await api('/publications/'+id+'/submit','POST',{confirmed:true});
      toast('已提交发布，请查看发布记录');
    }else if(action==='retry'){
      await api('/publications/'+id+'/retry','POST',{confirmed:true});
      toast('已重新排队；发布前会再次校验版本、审批与来源');
    }else{
      await api('/publications/'+id+'/check','POST',{});
      toast('已请求查询发布状态');
    }
    await refresh();
  }catch(error){toast(error.message);}
}
async function publishNow(draft){
  const confirmBox=$('pub-confirm');
  if(!confirmBox||!confirmBox.checked){toast('请先勾选确认当前版本可以发布');return;}
  try{
    await api('/drafts/'+draft.id+'/publish','POST',{revision:draft.revision,confirmed:true});
    toast('已排队发布。提交受理不等于已经上线，请查看发布记录');
    await refresh();
  }catch(error){toast(error.message);}
}
function renderDraftPublish(){
  const draft=selected;
  if(!draft)return;
  const existing=document.getElementById('publish-block');
  if(existing)existing.remove();
  const summary=(state&&state.publication)||{};
  const config=summary.config||{};
  const account=summary.account||{};
  const record=latestPublication(draft.id);
  let html='<div class="review-block" id="publish-block"><h3>公众号发布</h3>';
  html+='<p class="small">发布开关：'+(config.enabled?esc(({auto:'自动发布',draft:'仅保存草稿',manual:'仅手动发布'})[config.mode]||'仅手动发布'):'未启用')+' · 账号：'+esc(account.appid_masked||'未填写')+'</p>';
  if(draft.published_url||draft.status==='published'){
    html+='<p>已发布到公众号。'+(draft.published_url
      ?'<a href="'+esc(draft.published_url)+'" target="_blank" rel="noopener noreferrer">打开文章 ↗</a>'
      :'平台没有返回文章链接，请在公众号后台核对。')+'</p>';
  }else if(record){
    html+='<p>'+publishBadge(record.status)+' <span class="small">v'+esc(record.revision||'')+' · 尝试 '+esc(record.attempt||0)+'</span></p>';
    if(record.attention)html+='<p class="error">'+esc(publishAttention[record.attention]||record.attention)+'</p>';
    if(record.error)html+='<p class="error">'+esc(record.error)+'</p>';
    if(record.article_url)html+='<p><a href="'+esc(record.article_url)+'" target="_blank" rel="noopener noreferrer">打开公众号文章 ↗</a></p>';
    html+='<div class="decision-row">'+publishActions(record)+'</div>';
  }else if(draft.status==='ready_to_publish'&&config.enabled&&config.mode==='draft'){
    html+='<p class="muted">点击上方“一键提交微信草稿”保存当前版本，正式发表由你在公众号后台完成。</p>';
  }else if(draft.status==='ready_to_publish'&&config.enabled){
    html+='<label class="checkbox"><input id="pub-confirm" type="checkbox"><span>我已核对当前版本、标题、摘要与封面，确认现在发布到公众号。</span></label>';
    html+='<button id="pub-now" class="primary">立即发布到公众号</button>';
  }else if(draft.status==='ready_to_publish'){
    html+='<p class="muted">发布未启用：可在“发布到公众号”页面配置账号、封面与署名后开启；当前版本仍可人工发表并登记链接。</p>';
  }else{
    html+='<p class="muted">只有人工确认（待人工发表）的当前版本可以自动发布。</p>';
  }
  html+='</div>';
  $('draft-details').insertAdjacentHTML('afterbegin',html);
  bindPublishActions($('publish-block'));
  const button=$('pub-now');
  if(button)button.onclick=()=>publishNow(draft);
}

$('publish-form').addEventListener('input',()=>{publishDirty=true;});
$('publish-form').addEventListener('change',()=>{publishDirty=true;});
$('publish-form').onsubmit=async event=>{
  event.preventDefault();
  if(publishBusy||!publishLoaded)return;
  publishBusy=true;$('publish-save').disabled=true;
  try{
    await api('/publication','PUT',{enabled:$('pub-enabled').checked,mode:$('pub-mode').value,
      author:$('pub-author').value.trim(),auto_approve:$('pub-auto-approve').checked,cover_media_id:$('pub-cover-id').value.trim(),
      cover_path:$('pub-cover-path').value.trim(),need_open_comment:$('pub-comment').checked?1:0,
      only_fans_can_comment:$('pub-fans-comment').checked?1:0});
    publishDirty=false;
    toast('发布设置已保存，没有发送微信请求');
    await refresh();
  }catch(error){
    $('publish-status').textContent=error.message;
    $('publish-status').className='connection-result connection-failure';
  }finally{
    publishBusy=false;$('publish-save').disabled=false;
  }
};
$('pub-capability').onclick=async()=>{
  const button=$('pub-capability');
  button.disabled=true;
  $('pub-capability-result').textContent='正在检测（只读，不创建草稿）…';
  $('pub-capability-result').className='connection-result';
  try{
    const result=await api('/publication/capability','POST');
    const checks=result.checks||{};
    $('pub-capability-result').textContent=result.summary+' · 草稿接口：'+capabilityText(checks.draft)
      +' · 素材接口：'+capabilityText(checks.material)+' · 检测时间：'+time(result.checked_at);
    $('pub-capability-result').className='connection-result '+(result.publish_available?'connection-success':'connection-failure');
    await refresh();
  }catch(error){
    $('pub-capability-result').textContent=error.message;
    $('pub-capability-result').className='connection-result connection-failure';
  }finally{button.disabled=false;}
};

const renderDeskBeforePublish=renderDesk;
renderDesk=function(){
  renderDeskBeforePublish();
  if(state&&state.publication){applyPublishSummary(state.publication);renderPublishRecords();}
};
const showDraftBeforePublish=showDraft;
showDraft=async function(id,reset=true){await showDraftBeforePublish(id,reset);renderDraftPublish();};
const navigateBeforePublish=navigate;
navigate=function(tab){
  navigateBeforePublish(tab);
  if(tab==='publish'){
    $('page-title').textContent='发布到公众号';
    if(state&&state.publication){applyPublishSummary(state.publication);renderPublishRecords();}
  }
};
publishNav.onclick=()=>navigate('publish');
