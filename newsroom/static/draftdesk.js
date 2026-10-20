// The desktop workflow ends at draft storage; legacy publication records remain inspectable.
stages.prepare_article='素材提炼＋整篇策划（合并请求）';
stages.draft_format_retry='自动修正草稿格式';
labels.ready_to_publish='待存微信草稿';
const batchPanel=document.createElement('div');batchPanel.className='panel';
batchPanel.innerHTML='<h2>微信草稿交付</h2><p class="muted">一次提交全部尚未入库的真实稿件。先自动适配标题、摘要、排版与配图；已建草稿复用记录，结果未知先核对。最后一步只建立草稿，正式发表由你在公众号后台完成。</p><button id="draft-batch" class="primary">一键提交全部未提交稿件</button><div id="draft-batch-result" role="status" aria-live="polite"></div>';
controlPanel.before(batchPanel);
let batchBusy=false;
function unsubmittedDrafts(){
  return state.drafts.filter(d=>!d.demo&&!['rejected','manual_published','published','submitted','publish_unknown'].includes(d.status)&&!state.publications.some(p=>p.account_current&&p.draft_id===d.id&&p.revision===d.revision&&p.media_id&&['draft_only','draft_created','published'].includes(p.status)));
}
async function submitAllDrafts(){
  if(batchBusy)return;
  if(dirty&&$('draft-dialog').open){toast('请先保存当前编辑，再批量提交');return;}
  batchBusy=true;
  try{
    const batch=await api('/drafts/wechat-batch','POST',{revision:1,confirmed:true});
    toast('已排队处理 '+batch.total+' 篇稿件'+(state.workflow_control?.dispatch_paused?'；采编调度已暂停，请点击继续':'')+'，不会正式发表');
    await refresh();
  }catch(error){toast(error.message);}finally{batchBusy=false;}
}
$('draft-batch').onclick=submitAllDrafts;
// Replace the old per-article one-click affordance with one batch entry in the dialog.
draftButton=function(){return null;};
const deskBeforeDrafts=renderDesk;
renderDesk=function(){
  deskBeforeDrafts();
  const count=unsubmittedDrafts().length;
  $('draft-batch').textContent='一键提交全部未提交稿件（'+count+' 篇）';
  const active=(state.draft_batches||[]).find(b=>['queued','running','paused'].includes(b.status)&&state.jobs.some(j=>j.id===b.job_id&&['queued','running','paused'].includes(j.status)));
  $('draft-batch').disabled=!state.publication?.ready||Boolean(active)||batchBusy||count===0;
  $('draft-batch-result').innerHTML=(state.draft_batches||[]).slice(0,3).map(b=>'<p>'+badge(b.status)+' '+esc(time(b.created_at))+' · 已建 '+b.saved+' / '+b.total+' 篇 · 失败 '+b.failed+' · 待处理 '+(b.blocked||0)+' · 待核对 '+b.needs_check+'</p>'+(b.blocker?'<p class="error">本批次已停止：'+esc(b.blocker.error)+'。修复后再次点击提交，已成功稿件会跳过。</p>':'')+b.entries.filter(e=>['failed','needs_check'].includes(e.status)).map(e=>'<p class="error">'+esc(e.title)+'：'+esc(e.error||'请查看草稿记录')+'</p>').join('')).join('');
  if(state.settings.drafts_only){
    $('mode').textContent='自动采编 · 微信草稿交付';
    $('notice').textContent='署名：AI 编辑部 · 素材与策划合并请求 · 整篇写作 · 自动封面与图文 · 流程止于建立微信草稿';
    const saved=state.publications.filter(p=>p.account_current&&p.status==='draft_only').length;
    $('stats').innerHTML=[['新闻线索',state.story_total],['待存草稿',count],['已建草稿',saved]].map(([name,value])=>'<div class="stat"><small>'+name+'</small><strong>'+value+'</strong></div>').join('');
    document.querySelectorAll('.steps span').forEach(span=>{if(span.textContent==='人工审核发表')span.textContent='建立微信草稿';});
  }
  const last=state.usage?.image_requests||0;
  $('usage-summary').textContent+=' · 生图请求 '+last+' 次（按图片服务账单计费）';
  if(selected&&$('draft-dialog').open)renderBatchInDialog(count);
};
function renderBatchInDialog(count){
  document.querySelector('.approval-box').hidden=Boolean(state.settings.drafts_only);
  let button=$('draft-batch-dialog');
  if(!button){button=document.createElement('button');button.id='draft-batch-dialog';button.className='primary';$('publish-block').prepend(button);}
  button.textContent='一键提交全部未提交稿件（'+count+' 篇）';button.disabled=!state.publication?.ready||batchBusy||count===0;button.onclick=submitAllDrafts;
  const draft=state.drafts.find(d=>d.id===selected?.id);
  const asset=draft?.visuals?.assets?.find(a=>a.id===draft.visuals.cover_asset_id);
  let preview=$('draft-cover-preview');
  if(!preview&&asset){preview=document.createElement('div');preview.id='draft-cover-preview';$('publish-block').append(preview);}
  if(preview&&asset)preview.innerHTML='<p class="small">自动封面 · '+esc(asset.title)+'</p><img style="width:100%;height:auto" alt="自动选择的封面" src="/media/'+encodeURIComponent(asset.file)+'">';
  if(preview&&!asset)preview.innerHTML='';
  let imageTools=$('draft-image-tools');
  if(!imageTools){imageTools=document.createElement('div');imageTools.id='draft-image-tools';$('publish-block').append(imageTools);}
  const quality=draft?.visuals?.quality,plan=draft?.editorial_plan;
  const originals=(draft?.visuals?.assets||[]).filter(a=>['official_image','media_image','benchmark_image'].includes(a.kind)).length;
  const illustrations=(draft?.visuals?.assets||[]).filter(a=>a.kind==='illustration').length;
  imageTools.innerHTML='<p>实际新闻图片：'+originals+' 张'+(illustrations?' · 概念插画 '+illustrations+' 张':'')+(quality?' / 建议 '+quality.target_images+' 张':'')+'；文字卡片不计作新闻配图。</p>'+
    (plan?.actual_chars?'<p>正文 '+plan.actual_chars+' 字 / 目标约 '+plan.target_chars+' 字'+(plan.length_status==='short'?' · 篇幅偏短':'')+'</p>':'')+
    '<button id="refresh-news-images" class="secondary">重新搜集原图（不改写正文、无模型费用）</button>'+
    '<button id="fill-illustrations" class="secondary">按缺口补齐主题插画（调用生图，有费用）</button>'+
    (draft?.visuals?.error?'<p class="small">'+esc(draft.visuals.error)+'</p>':'')+
    '<details><summary>图片下载记录</summary><pre>'+esc(JSON.stringify(draft?.visuals?.source_images||{},null,2))+'</pre></details>';
  $('refresh-news-images').disabled=state.jobs.some(j=>j.kind==='images'&&j.subject===draft?.id&&['queued','running','paused'].includes(j.status));
  $('refresh-news-images').onclick=async()=>{
    if(dirty){toast('请先保存正文编辑，再重新搜集图片');return;}
    try{await api('/drafts/'+draft.id+'/images/refresh','POST',{revision:draft.revision});toast('已排队重新搜集实际图片；不会调用文字／生图模型，不会提交微信');await refresh();}catch(error){toast(error.message);}
  };
  const fillButton=$('fill-illustrations');
  if(fillButton){
    const busy=state.jobs.some(j=>j.kind==='illustrations'&&j.subject===draft?.id&&['queued','running','paused'].includes(j.status));
    fillButton.disabled=!state.settings.image_enabled||busy;
    fillButton.title=state.settings.image_enabled?'按整篇策划补齐缺失插图；只生成缺的部分':'生图未启用：在 API 与网络设置中配置图片服务';
    fillButton.onclick=async()=>{
      if(dirty){toast('请先保存正文编辑，再补齐插画');return;}
      if(!confirm('会调用生图服务并产生费用，只补齐缺失的插图。继续？'))return;
      try{await api('/drafts/'+draft.id+'/illustrations','POST',{revision:draft.revision,confirmed:true});toast('已排队补齐插画：只生成缺失部分，不提交微信');await refresh();}catch(error){toast(error.message);}
    };
  }
  let deleteBox=$('draft-delete-box');
  if(!deleteBox){deleteBox=document.createElement('div');deleteBox.id='draft-delete-box';$('publish-block').appendChild(deleteBox);}
  const activePublication=(state.publications||[]).some(p=>p.draft_id===draft?.id&&['prepared','draft_created','submitting','accepted','publishing','unknown'].includes(p.status));
  const busyJob=state.jobs.some(j=>j.subject===draft?.id&&['queued','running','paused'].includes(j.status));
  deleteBox.innerHTML='<button id="delete-draft" class="text-button">删除此稿（只删本地记录与交稿包）</button>';
  const deleteButton=$('delete-draft');
  deleteButton.disabled=activePublication||busyJob;
  deleteButton.title=activePublication?'有进行中的发布记录，请先对账或放弃':busyJob?'有排队或运行中的任务，请等结束后再删':'公众号草稿与已发表文章不会被删除';
  deleteButton.onclick=async()=>{
    const published=draft.status==='published'||draft.published_url;
    const note=published?'\n该稿已发布到公众号：删除只删本地记录，公众号文章仍然存在。':'';
    if(!confirm('删除稿件「'+(draft.article?.title||draft.id)+'」？本地记录与交稿包会被删除，无法撤销。'+note+'\n公众号草稿箱里已建立的草稿不会被删除。'))return;
    try{await api('/drafts/'+draft.id+'/delete','POST',{confirmed:true});$('draft-dialog').close();selected=null;toast('已删除该稿件');await refresh();}catch(error){toast(error.message);}
  };
}
const showBeforeDrafts=showDraft;
showDraft=async function(id,reset=true){await showBeforeDrafts(id,reset);renderBatchInDialog(unsubmittedDrafts().length);};
publishNav.textContent='⇪　微信草稿交付';
const navigateBeforeDraftDesk=navigate;
navigate=function(tab){navigateBeforeDraftDesk(tab);if(tab==='publish')$('page-title').textContent='微信草稿交付';};
$('pub-mode').innerHTML='<option value="draft">仅建立草稿，人工在公众号后台发表</option>';
$('pub-mode').previousElementSibling.textContent='交付方式';
const coverSelect=document.createElement('label');coverSelect.innerHTML='封面方式<select id="pub-cover-mode"><option value="auto">自动选择本条新闻最有代表性的原图（缺图提示补图）</option><option value="generated">优先生成主题封面（需配置生图服务，失败用原图／信息图）</option><option value="manual">手动固定封面</option></select>';
$('pub-cover-id').parentElement.insertBefore(coverSelect,$('pub-cover-id').previousElementSibling);
const applyBeforeDrafts=applyPublishSummary;
applyPublishSummary=function(summary){applyBeforeDrafts(summary);if(!publishDirty)$('pub-cover-mode').value=summary.config.cover_mode||'auto';$('publish-status').textContent=(summary.ready?'草稿配置就绪':'待完善：'+summary.problems.join('；'))+' · 署名 '+summary.config.author+' · 仅建立草稿';};
$('publish-form').onsubmit=async event=>{
  event.preventDefault();if(publishBusy)return;publishBusy=true;
  try{await api('/publication','PUT',{enabled:$('pub-enabled').checked,mode:'draft',author:$('pub-author').value.trim(),auto_approve:$('pub-auto-approve').checked,
    cover_mode:$('pub-cover-mode').value,cover_media_id:$('pub-cover-id').value.trim(),cover_path:$('pub-cover-path').value.trim(),need_open_comment:$('pub-comment').checked?1:0,only_fans_can_comment:$('pub-fans-comment').checked?1:0});publishDirty=false;toast('草稿配置已保存，未调用微信');await refresh();}catch(error){toast(error.message);}finally{publishBusy=false;}
};
const actionsBeforeDrafts=publishActions;
publishActions=function(record){return actionsBeforeDrafts(record).replace(/<button[^>]+data-pub-action="(?:submit|resume)"[^>]*>[^<]*<\/button>/g,'');};
const imagePanel=document.createElement('section');imagePanel.className='panel';
imagePanel.innerHTML='<h2>可选生图 API 与合并处理</h2><p class="muted">默认不启用付费生图：优先新闻原图和正文链接的一手资料，目标按篇幅选择4至8张不同实际图片。缺图明确提示，不用文字卡片凑数。启用后，仅缺少新闻图片或选择生成封面时请求一张，并复用缓存；失败不自动重试付费请求。支持 MiniMax 原生 image_generation 与 images/generations 兼容接口，自动识别完整地址，避免重复拼接路径。</p><form id="image-config"><label class="checkbox"><input id="image-enabled" type="checkbox"><span>启用可选生图 API（可能按图片收费）</span></label><label>HTTPS 服务地址（可填完整生图接口）<input id="image-base" placeholder="https://服务地址/v1"></label><label>模型 ID<input id="image-model"></label><label>图片尺寸<input id="image-size" value="1024x1024" placeholder="1024x1024 或 auto"></label><label>API Key（留空保留）<input id="image-key" type="password" autocomplete="new-password"></label><label class="checkbox"><input id="image-clear" type="checkbox"><span>清除已保存的生图密钥</span></label><label>本机 HTTP 代理（留空沿用图文服务路线）<input id="image-proxy" placeholder="http://127.0.0.1:7897"></label><label class="checkbox"><input id="image-merged" type="checkbox" checked><span>合并素材提炼与整篇策划，减少文字请求（推荐）</span></label><p id="image-config-status" class="small"></p><button class="secondary" type="submit">保存生图与合并设置</button></form>';
settingsPage.append(imagePanel);
const imageProtocolLabel=document.createElement('label');
imageProtocolLabel.innerHTML='生图接口协议<select id="image-provider"><option value="auto">自动识别（推荐，兼容旧配置）</option><option value="minimax">MiniMax 原生 image_generation</option><option value="openai_compatible">images/generations 兼容接口</option></select>';
$('image-base').parentElement.before(imageProtocolLabel);
let imageDirty=false;
$('image-config').oninput=()=>{imageDirty=true;};
const configBeforeImages=applyRuntimeConfig;
applyRuntimeConfig=function(config){
  configBeforeImages(config);if(imageDirty)return;
  const image=config.image_generation||{};$('image-provider').value=image.provider||'auto';$('image-enabled').checked=Boolean(image.enabled);$('image-base').value=image.base_url||'';$('image-model').value=image.model||'';$('image-size').value=image.size||'1024x1024';$('image-proxy').value=image.proxy_url||'';$('image-key').value='';$('image-clear').checked=false;$('image-merged').checked=config.merged_preparation!==false;$('image-config-status').textContent=(image.api_key_configured?'生图密钥已保存，不回显；保存不会发送生图请求。':'生图未配置，使用原图；不足时可零模型费用重新搜集，仍缺图会明确提示。')+(image.endpoint?' 实际请求：'+image.resolved_provider+' · '+image.endpoint:'')+(image.last_result?' 上次生图（'+time(image.last_result.at)+'）：'+(image.last_result.outcome==='ok'?'成功':image.last_result.error||'未完成'):'');
};
$('image-config').onsubmit=async event=>{event.preventDefault();try{const result=await api('/config','PUT',{merged_preparation:$('image-merged').checked,image_generation:{provider:$('image-provider').value,enabled:$('image-enabled').checked,base_url:$('image-base').value.trim(),model:$('image-model').value.trim(),size:$('image-size').value.trim(),proxy_url:$('image-proxy').value.trim(),api_key:$('image-key').value,clear_api_key:$('image-clear').checked}});imageDirty=false;applyRuntimeConfig(result);toast('生图设置已保存，未发送付费请求');}catch(error){toast(error.message);}};
