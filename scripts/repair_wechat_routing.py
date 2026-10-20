"""零新增费用的 Clash Verge 微信分流修复；不测试微信、不修改凭证或节点。"""
import argparse
import http.client
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

RULES = ['DOMAIN,api.weixin.qq.com,DIRECT', 'DOMAIN,mp.weixin.qq.com,DIRECT',
         'DOMAIN-SUFFIX,weixin.qq.com,DIRECT']


class PipeSocket:
    def __init__(self, path):
        self.file = open(path, 'r+b', buffering=0)

    def makefile(self, *args, **kwargs):
        # HTTPConnection can close its socket before the response body is read.
        return os.fdopen(os.dup(self.file.fileno()), 'rb', buffering=0)

    def sendall(self, data):
        self.file.write(data)

    def close(self):
        self.file.close()


def request(pipe, method, route, body=None):
    class PipeHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = PipeSocket(pipe)
    client = PipeHTTP('localhost', timeout=10)
    try:
        client.request(method, route, body=json.dumps(body) if body is not None else None,
                       headers={'Connection': 'close', 'Content-Type': 'application/json'})
        response = client.getresponse()
        data = response.read()
        if response.status not in {200, 204}:
            raise RuntimeError('Clash 本机控制接口返回 HTTP ' + str(response.status))
        return json.loads(data) if data else {}
    finally:
        client.close()


def prepend_rules(text):
    # Only accept a block rules list. Do not reinterpret or rewrite the subscription YAML.
    match = re.search(r'^rules:[ \t]*$', text, re.M)
    if not match:
        raise RuntimeError('未识别 Clash 规则列表，未改写订阅；请在 Clash 中重新应用当前订阅')
    before, after = text[:match.end()], text[match.end():]
    for rule in RULES:
        after = re.sub(r'(?m)^[ \t]*-[ \t]*' + re.escape(rule) + r'[ \t]*\n?', '', after)
    item = re.search(r'^([ \t]*)- ', after, re.M)
    indent = item.group(1) if item else ''
    return before + '\n' + '\n'.join(indent + '- ' + rule for rule in RULES) + after


def check_script(existing):
    if existing.startswith('// AI Newsroom: persistent WeChat DIRECT rules v1'):
        return
    plain = re.sub(r'//[^\n]*', '', existing).strip()
    if not re.fullmatch(r'function\s+main\(config(?:,\s*profileName)?\)\s*\{\s*return config;?\s*\}', plain):
        raise RuntimeError('已有自定义全局脚本，为保留你的配置未覆盖；请合并 scripts/clash_wechat_rules.js 中的规则')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='备份后持久化并应用微信直连规则；默认只读')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('此脚本针对 Windows Clash Verge Rev')
    root = Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming'))) / 'io.github.clash-verge-rev.clash-verge-rev'
    pipes = [p for p in os.listdir('//./pipe/') if p.startswith('verge-mihomo-')]
    if len(pipes) != 1:
        raise RuntimeError('未找到唯一运行中的 Clash 内核，请先打开 Clash Verge')
    pipe = '//./pipe/' + pipes[0]
    current = request(pipe, 'GET', '/configs')
    if args.apply:
        generated = root / 'clash-verge.yaml'
        script = root / 'profiles/Script.js'
        check_script(script.read_text(encoding='utf-8'))
        changes = {generated: prepend_rules(generated.read_text(encoding='utf-8')),
                   script: Path(__file__).with_name('clash_wechat_rules.js').read_text(encoding='utf-8')}
        for file in [root / 'config.yaml', generated]:
            value = changes.get(file, file.read_text(encoding='utf-8'))
            if not re.search(r'^mode:', value, re.M):
                raise RuntimeError('未识别 Clash 运行模式；未保存修复')
            changes[file] = re.sub(r'^mode:.*$', 'mode: rule', value, count=1, flags=re.M)
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
        backups = {}
        for path in changes:
            backups[path] = path.with_name(path.name + '.newsroom-routing-backup-' + stamp)
            shutil.copy2(path, backups[path])
        # First ask the core to validate/apply; invalid YAML must never replace saved files.
        # 服务模式内核的工作目录可能不同，用 payload 避免路径白名单差异。
        request(pipe, 'PUT', '/configs?force=false', {'payload': changes[generated]})
        try:
            request(pipe, 'PATCH', '/configs', {'mode': 'rule'})
            for path, content in changes.items():
                temp = path.with_name(path.name + '.newsroom-routing.tmp')
                temp.write_text(content, encoding='utf-8')
                temp.replace(path)
        except (OSError, RuntimeError, http.client.HTTPException):
            try:
                for path, backup in backups.items():
                    shutil.copy2(backup, path)
                request(pipe, 'PUT', '/configs?force=false', {'payload': backups[generated].read_text(encoding='utf-8')})
                request(pipe, 'PATCH', '/configs', {'mode': current.get('mode', 'rule')})
            except (OSError, RuntimeError, http.client.HTTPException):
                raise RuntimeError('保存失败且未完整恢复，请使用 Clash 目录的 newsroom-routing-backup 备份恢复') from None
            raise RuntimeError('保存失败，已恢复原文件和运行配置') from None
        current = request(pipe, 'GET', '/configs')
    rules = request(pipe, 'GET', '/rules').get('rules', [])
    leading = [{'type': r.get('type'), 'payload': r.get('payload'), 'proxy': r.get('proxy')} for r in rules[:3]]
    ready = (current.get('mode') == 'rule' and len(leading) == 3 and
             [r['payload'] for r in leading] == ['api.weixin.qq.com', 'mp.weixin.qq.com', 'weixin.qq.com'] and
             all(r['proxy'] == 'DIRECT' for r in leading))
    print(json.dumps({'applied': args.apply, 'wechat_direct_rules_verified': ready, 'mode': current.get('mode'),
                      'mixed_port': current.get('mixed-port'), 'tun_enabled': current.get('tun', {}).get('enable'),
                      'leading_rules': leading, 'public_ip_fixed': False}, ensure_ascii=False, indent=2))
    if args.apply and not ready:
        raise RuntimeError('规则已保存，但运行规则核验未通过；请在 Clash 重新应用订阅后复查')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        print('微信分流修复未完成：' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
        raise SystemExit(1)
