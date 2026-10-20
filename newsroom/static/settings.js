// 固定模板；所有服务端配置和错误仅写入 value/textContent，不拼进 HTML。
const settingsNav=document.createElement('button');
settingsNav.className='nav';settingsNav.dataset.tab='settings';settingsNav.textContent='⚙　API 与网络设置';
document.querySelector('nav').insertBefore(settingsNav,document.querySelector('[data-tab="wechat"]'));
const settingsPage=document.createElement('section');settingsPage.id='settings';settingsPage.className='tab';settingsPage.hidden=true;
settingsPage.innerHTML=`
<form id="runtime-form" autocomplete="off">
  <div class="panel config-intro"><div><p class="eyebrow">REAL WORKSPACE</p><h2>配置服务，开始真实采编</h2><p class="muted">保存后立即生效并跨重启保留；保存不会调用模型或微信。密钥留空保留原值，不回显已保存密钥。</p></div><button type="submit" id="config-save" class="primary">保存全部配置</button></div>
  <div id="config-result" class="connection-result" role="status" aria-live="polite">正在读取本机配置…</div>
  <div class="two-columns config-section">
    <div class="panel"><h2>运行模式</h2><label for="config-mode">采编方式</label><select id="config-mode"><option value="demo">演示采编（不调用真实模型）</option><option value="real">真实采编（使用 API，可能产生费用）</option></select><p class="muted">切换真实采编后，每日计划与手动运行均会使用真实模型。系统会在保存前检查当前默认小编及审核服务的配置完整性；完整不代表已验证网络、余额或模型能力。</p><label class="checkbox"><input type="checkbox" id="config-json"><span>模型支持 JSON Object 模式</span></label><label for="config-max-tokens">单次输出 token（0＝不设应用上限）</label><input id="config-max-tokens" type="number" min="0" required><p class="muted">设为 0 时不传 max_tokens；遇到服务商截断会请求接续。服务商的上下文与输出能力、微信接口本身的上限仍然适用。图文服务留空可复用写作模型，也可配置独立服务。</p></div>
    <div class="panel"><h2>微信公众号凭证</h2><label for="config-appid">AppID</label><input id="config-appid" autocomplete="off" spellcheck="false" maxlength="100"><label for="config-appsecret">AppSecret <span id="config-secret-state" class="muted"></span></label><input id="config-appsecret" type="password" autocomplete="new-password" maxlength="4096" placeholder="留空保留已保存值"><label class="checkbox"><input id="config-clear-secret" type="checkbox"><span>明确清除已保存 AppSecret</span></label><p class="muted">微信请求固定应用层直连，不使用下方代理；TUN 仍可能接管网络。测试请先保存，再去“公众号连接”点击。</p><button type="button" id="config-open-wechat" class="secondary">前往微信连接测试</button></div>
  </div>
  <div class="panel config-section"><p class="eyebrow">MODEL SERVICES</p><h2>四个模型服务，分别配置网络</h2><p class="muted">支持与 Chat Completions 兼容的接口。填写服务商给出的 Base URL（如需要 /v1 请保留），不要填写完整 /chat/completions。自主策划使用这里配置的提示词模型，整篇正文使用写作模型；无需手选小编。</p><div id="config-models" class="config-model-grid"></div></div>
  <div class="panel config-section"><p class="eyebrow">SPLIT ROUTING</p><h2>新闻走代理，微信走直连</h2><label class="checkbox"><input type="checkbox" id="config-news-proxy-enabled"><span>新闻 RSS、原文、来源复查与固定 DNS 校验使用本机代理</span></label><label for="config-news-proxy">Clash HTTP／混合代理地址</label><div class="config-proxy-row"><input id="config-news-proxy" placeholder="http://127.0.0.1:你的混合端口" autocomplete="off" spellcheck="false"><button type="button" id="config-use-proxy-hint" class="secondary" disabled>填入检测到的本机端口</button></div><p id="config-proxy-hint" class="muted"></p><p class="muted">查看 Clash Verge“设置”中的 HTTP／混合端口，不能填写 SOCKS 端口。每个模型可独立选择直连或本机代理；国内模型服务通常可直连。所有连接仍校验 HTTPS 证书。</p>
  <div class="quality-note"><strong>针对你的 Clash Verge：全局 + TUN 不能靠应用参数绕过</strong><p>方案 A：关闭“虚拟网卡模式 / TUN”，保持 Clash 客户端运行，将这里的新闻代理设为它的 HTTP／混合端口。新闻和选定模型通过该端口，微信直接访问。其他应用需要代理时，可以使用 Clash 的系统代理。</p><p>方案 B：保留 TUN，将“全局”改成“规则”，把下方微信直连规则放在订阅规则前面，并确认已生效。若使用 Fake-IP，再按文档添加 DNS 过滤。全局模式不会按这些自定义分流规则执行。</p></div>
  <pre id="config-clash-rules" class="prompt-view">rules:
  - DOMAIN,api.weixin.qq.com,DIRECT
  - DOMAIN,mp.weixin.qq.com,DIRECT</pre><button type="button" id="config-copy-rules" class="secondary">复制微信直连规则</button><p class="muted">这是需要合并到订阅前面的规则片段，不要替换完整订阅；系统不会自动修改 Clash、路由或虚拟网卡。切换出口后若微信返回 IP 白名单错误，需要在微信后台配置实际直连出口 IP。</p></div>
  <div class="config-bottom"><button type="submit" class="primary">保存全部配置</button><button type="button" id="config-reload" class="secondary">重新读取（放弃未保存内容）</button></div>
</form>`;
document.querySelector('main').insertBefore(settingsPage,document.querySelector('main footer'));
const configRoles={writer:'正文写作与选题',prompt:'责任编辑与提示词策划',review:'事实对照与 AI 审核',visual:'官方原图选配与图文排版'};
for(const [role,name]of Object.entries(configRoles)){
  const card=document.createElement('div');card.className='config-model-card';
  card.innerHTML=`<h3>${name}</h3>${role!=='writer'?`<label class="checkbox"><input type="checkbox" id="cfg-${role}-reuse"><span>复用写作服务的地址和密钥</span></label>`:''}
  <label for="cfg-${role}-base">Base URL</label><input id="cfg-${role}-base" type="url" placeholder="https://provider.example/v1" maxlength="500" spellcheck="false">
  <label for="cfg-${role}-model">模型 ID</label><input id="cfg-${role}-model" maxlength="200" placeholder="服务商提供的模型 ID" spellcheck="false">
  <label for="cfg-${role}-key">API Key <span id="cfg-${role}-key-state" class="muted"></span></label><input id="cfg-${role}-key" type="password" autocomplete="new-password" maxlength="4096" placeholder="留空保留已保存值">
  <label class="checkbox"><input id="cfg-${role}-clear" type="checkbox"><span>明确清除此服务密钥</span></label>
  <label for="cfg-${role}-route">此服务网络</label><select id="cfg-${role}-route"><option value="direct">应用层直连</option><option value="proxy">指定本机 HTTP／混合代理</option></select>
  <input id="cfg-${role}-proxy" aria-label="${name}代理地址" placeholder="http://127.0.0.1:端口" spellcheck="false">
  <button type="button" id="cfg-${role}-test" class="secondary config-test">测试已保存的模型服务</button><p id="cfg-${role}-test-result" class="muted" role="status">点击将发送一次真实小请求，可能产生少量费用。</p>`;
  $('config-models').append(card);
}
$('config-mode').insertAdjacentHTML('afterend','<label for="config-review-mode">独立 AI 复审</label><select id="config-review-mode"><option value="off">关闭：程序检查后交稿（省费用）</option><option value="advisory">建议：AI 给意见，不因评分拦稿</option><option value="strict">严格：AI 通过后交稿</option></select>');
$('config-mode').insertAdjacentHTML('afterend','<label class="checkbox"><input type="checkbox" id="config-fast"><span>快速采编：批量评分、整篇写作、自动原图排版；MiMo官方接口关闭深度思考并使用JSON输出</span></label>');
let runtimeLoaded=false,runtimeDirty=false,runtimeBusy=false,runtimeSnapshot=null;
function updateConfigFields(){
  $('config-news-proxy').disabled=!$('config-news-proxy-enabled').checked;
  for(const role of Object.keys(configRoles)){
    const reuse=role!=='writer'&&$('cfg-'+role+'-reuse').checked;
    for(const field of ['base','key','clear'])$('cfg-'+role+'-'+field).disabled=reuse;
    $('cfg-'+role+'-proxy').disabled=$('cfg-'+role+'-route').value!=='proxy';
  }
}
function configMessage(message,error=false){$('config-result').textContent=message;$('config-result').className='connection-result '+(error?'connection-failure':'');}
function applyRuntimeConfig(data){
  runtimeSnapshot=data;$('config-fast').checked=data.fast_mode!==false;$('config-review-mode').value=data.review_mode||'off';$('config-mode').value=data.demo?'demo':'real';$('config-json').checked=data.json_mode;$('config-max-tokens').value=data.max_output_tokens;
  $('config-appid').value=data.wechat.app_id;$('config-appsecret').value='';$('config-clear-secret').checked=false;$('config-secret-state').textContent=data.wechat.secret_configured?'已保存，不回显':'尚未保存';
  $('config-news-proxy-enabled').checked=Boolean(data.news_proxy_url);$('config-news-proxy').value=data.news_proxy_url||'';
  for(const role of Object.keys(configRoles)){
    const model=data.models[role];
    $('cfg-'+role+'-base').value=model.base_url;$('cfg-'+role+'-model').value=model.model;$('cfg-'+role+'-key').value='';$('cfg-'+role+'-clear').checked=false;
    $('cfg-'+role+'-key-state').textContent=model.api_key_configured?'已保存，不回显':'未单独保存';
    if(role!=='writer')$('cfg-'+role+'-reuse').checked=!model.base_url&&!model.api_key_configured;
    $('cfg-'+role+'-route').value=model.proxy_url?'proxy':'direct';$('cfg-'+role+'-proxy').value=model.proxy_url;
    $('cfg-'+role+'-test-result').textContent='点击将发送一次真实小请求，可能产生少量费用。';
  }
  $('config-use-proxy-hint').disabled=!data.proxy_hint;$('config-proxy-hint').textContent=data.proxy_hint?'系统代理记录中的本机地址：'+data.proxy_hint+'（尚未验证；点击填入后保存）':data.news_proxy_url?'已保存本机代理地址：'+data.news_proxy_url+'；端口以 Clash 设置为准。':'未从系统代理记录读到可用本机 HTTP 端口，请在 Clash Verge 设置中查看后填写。';
  configMessage((data.demo?'当前：演示采编。':'当前：真实采编。')+' 保存位置：'+data.storage+'。'+(data.ready?'模型字段完整，连接需手动测试。':'待完成：'+data.problems.join('；')));
  runtimeLoaded=true;runtimeDirty=false;updateConfigFields();
}
async function loadRuntimeConfig(force=false){
  if(runtimeDirty&&!force)return;
  try{applyRuntimeConfig(await api('/config'));}catch(error){configMessage(error.message,true);}
}
$('runtime-form').addEventListener('input',()=>{runtimeDirty=true;});
$('runtime-form').addEventListener('change',()=>{runtimeDirty=true;updateConfigFields();});
$('runtime-form').onsubmit=async event=>{
  event.preventDefault();if(runtimeBusy||!runtimeLoaded)return;
  const body={editorial_mode:'auto',fast_mode:$('config-fast').checked,review_mode:$('config-review-mode').value,demo:$('config-mode').value==='demo',json_mode:$('config-json').checked,max_output_tokens:Number($('config-max-tokens').value),
    wechat:{app_id:$('config-appid').value.trim(),app_secret:$('config-appsecret').value,clear_app_secret:$('config-clear-secret').checked},
    news_proxy_url:$('config-news-proxy-enabled').checked?$('config-news-proxy').value.trim():'',models:{}};
  for(const role of Object.keys(configRoles)){
    const reuse=role!=='writer'&&$('cfg-'+role+'-reuse').checked;
    body.models[role]={base_url:reuse?'':$('cfg-'+role+'-base').value.trim(),model:$('cfg-'+role+'-model').value.trim(),
      api_key:reuse?'':$('cfg-'+role+'-key').value,clear_api_key:reuse||$('cfg-'+role+'-clear').checked,
      proxy_url:$('cfg-'+role+'-route').value==='proxy'?$('cfg-'+role+'-proxy').value.trim():''};
    if($('cfg-'+role+'-route').value==='proxy'&&!body.models[role].proxy_url){configMessage('请填写所选模型服务的代理地址',true);return;}
  }
  if($('config-news-proxy-enabled').checked&&!body.news_proxy_url){configMessage('请填写 Clash HTTP／混合代理地址',true);return;}
  runtimeBusy=true;$('config-save').disabled=true;configMessage('正在保存本机配置…');
  try{applyRuntimeConfig(await api('/config','PUT',body));await refresh();await loadWechatStatus();toast('配置已保存并生效，没有自动发送连接测试');}
  catch(error){configMessage(error.message,true);}
  finally{runtimeBusy=false;$('config-save').disabled=false;}
};
for(const role of Object.keys(configRoles))$('cfg-'+role+'-test').onclick=async()=>{
  if(runtimeDirty){toast('请先保存配置，再测试已保存的服务');return;}
  const button=$('cfg-'+role+'-test');button.disabled=true;$('cfg-'+role+'-test-result').textContent='正在发送一次真实小请求…';
  try{const result=await api('/config/models/'+role+'/test','POST');$('cfg-'+role+'-test-result').textContent=(result.ok?'成功：':'未通过：')+result.message+' · 应用配置路由：'+(result.route==='proxy'?'代理':'直连');}
  catch(error){$('cfg-'+role+'-test-result').textContent=error.message;}
  finally{button.disabled=false;}
};
$('config-use-proxy-hint').onclick=()=>{if(runtimeSnapshot?.proxy_hint){$('config-news-proxy').value=runtimeSnapshot.proxy_hint;$('config-news-proxy-enabled').checked=true;runtimeDirty=true;updateConfigFields();}};
$('config-copy-rules').onclick=async()=>{try{await navigator.clipboard.writeText($('config-clash-rules').textContent);toast('规则已复制；请切换规则模式并合并到原规则前面');}catch{toast('无法写入剪贴板，请手动选中复制规则');}};
$('config-open-wechat').onclick=()=>{if(runtimeDirty){toast('请先保存配置');return;}navigate('wechat');};
$('config-reload').onclick=()=>loadRuntimeConfig(true);
const navigateBeforeSettings=navigate;
navigate=function(tab){navigateBeforeSettings(tab);if(tab==='settings'){$('page-title').textContent='API 与网络设置';loadRuntimeConfig();}};
settingsNav.onclick=()=>navigate('settings');
const configureWechatButton=document.createElement('button');configureWechatButton.type='button';configureWechatButton.className='secondary';configureWechatButton.textContent='配置／更新微信凭证';configureWechatButton.onclick=()=>navigate('settings');$('wechat-config-status').after(configureWechatButton);
const logoutBeforeSettings=$('logout').onclick;
$('logout').onclick=()=>{for(const field of settingsPage.querySelectorAll('input[type="password"]'))field.value='';runtimeLoaded=false;runtimeDirty=false;runtimeSnapshot=null;logoutBeforeSettings();};
