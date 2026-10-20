// AI Newsroom: persistent WeChat DIRECT rules v1
// Preserve subscription nodes, selected groups, DNS and TUN settings.
function main(config, profileName) {
  const direct = [
    'DOMAIN,api.weixin.qq.com,DIRECT',
    'DOMAIN,mp.weixin.qq.com,DIRECT',
    'DOMAIN-SUFFIX,weixin.qq.com,DIRECT'
  ];
  config.rules = direct.concat((config.rules || []).filter(rule => !direct.includes(rule)));
  return config;
}
