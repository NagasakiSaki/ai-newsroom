let wechatStatus=null,wechatTesting=false;
function renderWechat(){
  const s=wechatStatus;
  $('wechat-config-status').textContent=s?(s.configured?'凭证已配置 · 尚不代表连接有效':'凭证未配置完整'):'正在读取本机配置状态…';
  $('wechat-appid').textContent=s?.appid_masked||'—';
  $('wechat-secret-state').textContent=s?(s.secret_configured?'已填写（不显示密钥）':'未填写'):'—';
  const busy=wechatTesting||Boolean(s?.testing);
  $('test-wechat').disabled=busy||!s?.configured;
  $('test-wechat').textContent=busy?'正在测试，请稍候…':'一键测试微信连接';
  const result=s?.last_test;
  const box=$('wechat-test-result');
  box.className='connection-result '+(result?(result.ok?'connection-success':'connection-failure'):'');
  if(wechatTesting){box.textContent='正在向微信令牌接口发送一次请求，请等待结果。不会自动重试。';return;}
  box.replaceChildren();
  const title=document.createElement('strong');title.textContent=result?(result.ok?'连接测试成功':'连接测试未通过'):'尚未测试连接';box.append(title);
  const detail=document.createElement('p');detail.textContent=result?result.message:'等网络良好时点击按钮。打开此页面、刷新或定时采编均不会自动测试。';box.append(detail);
  if(result&&!result.ok&&result.wechat_errcode===40164){
    const help=document.createElement('div');help.className='wechat-whitelist-help';
    if(result.whitelist_ip){
      const label=document.createElement('label');label.htmlFor='wechat-whitelist-ip';label.textContent='需要加入白名单的 IP';help.append(label);
      const row=document.createElement('div');row.className='wechat-whitelist-row';
      const address=document.createElement('input');address.id='wechat-whitelist-ip';address.value=result.whitelist_ip;address.readOnly=true;address.spellcheck=false;address.onclick=()=>address.select();row.append(address);
      const copy=document.createElement('button');copy.type='button';copy.className='secondary';copy.textContent='复制 IP';
      copy.onclick=async()=>{try{await navigator.clipboard.writeText(address.value);toast('白名单 IP 已复制');}catch{address.focus();address.select();toast('已选中 IP，请按 Ctrl+C 复制');}};row.append(copy);help.append(row);
      const guide=document.createElement('p');guide.textContent='微信开发者平台 → 该公众号 → 基础信息 → API IP 白名单。保留原有条目，加入上面的 IP 并保存，然后重新测试。';help.append(guide);
      const note=document.createElement('p');note.className='muted';note.textContent='此地址来自微信本次错误响应。更换网络或出口 IP 后，请按最新测试结果更新白名单。';help.append(note);
    }else{
      const note=document.createElement('p');note.textContent='微信未返回可识别的公网 IP，本次无法确定应填写的地址；请重新测试。';help.append(note);
    }
    box.append(help);
  }
  if(result){const meta=document.createElement('p');meta.className='muted';meta.textContent='测试时间：'+time(result.checked_at)+' · 耗时 '+result.duration_ms+' ms'+(result.expires_in?' · 返回有效期 '+result.expires_in+' 秒':'')+(result.wechat_errcode!==null?' · 微信错误码 '+result.wechat_errcode:'')+(result.http_status!==null?' · HTTP '+result.http_status:'');box.append(meta);}
}
async function loadWechatStatus(){try{wechatStatus=await api('/wechat/status');renderWechat();}catch(error){$('wechat-config-status').textContent='读取配置状态失败：'+error.message;}}
$('test-wechat').onclick=async()=>{
  if(wechatTesting)return;
  wechatTesting=true;renderWechat();
  try{const result=await api('/wechat/test-connection','POST');wechatStatus={...wechatStatus,last_test:result,testing:false};}
  catch(error){toast(error.message);}
  finally{wechatTesting=false;await loadWechatStatus();renderWechat();}
};
const navigateBeforeWechat=navigate;
navigate=function(tab){navigateBeforeWechat(tab);if(tab==='wechat'){loadWechatStatus();}};
