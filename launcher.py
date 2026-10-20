"""双击入口：准备环境，后台启动编辑台；不更改系统定时任务或开机设置。"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8088"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health():
    try:
        with HTTP.open(URL + "/api/health", timeout=2) as response:
            return json.load(response).get("application") == "ai-newsroom"
    except (OSError, ValueError):
        return False


def browser_login_url(admin_token):
    request = urllib.request.Request(URL + '/api/desktop/login-ticket',
        headers={'Authorization': 'Bearer ' + admin_token}, method='POST')
    try:
        with HTTP.open(request, timeout=10) as response:
            data = json.load(response)
    except (OSError, ValueError):
        raise RuntimeError('无法创建启动登录链接，请先用 stop.cmd 停止旧版服务，再双击一键启动') from None
    ticket = data.get('ticket') if isinstance(data, dict) else None
    if not isinstance(ticket, str) or not ticket:
        raise RuntimeError('启动登录链接生成失败，请查看服务日志')
    # 短期票据仅放 fragment，不进入 HTTP 请求路径或普通服务访问日志。
    return URL + '/#' + urlencode({'desktop-login': ticket})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--run-now", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise RuntimeError('启动编辑台需要 Python 3.11 或更新版本')
    os.chdir(ROOT)
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    marker = ROOT / ".venv" / "newsroom-requirements.txt"
    lock = (ROOT / "requirements.lock.txt").read_bytes()
    if not marker.exists() or marker.read_bytes() != lock:
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.lock.txt")], check=True)
        marker.write_bytes(lock)
    if Path(sys.executable).resolve() != python.resolve():
        raise SystemExit(subprocess.call([str(python), __file__, *sys.argv[1:]], cwd=ROOT))
    if not (ROOT / ".env").exists():
        shutil.copyfile(ROOT / ".env.example", ROOT / ".env")
    from newsroom.config import Settings
    settings = Settings.load()
    if args.stop:
        if health():
            request = urllib.request.Request(URL + "/api/shutdown", headers={"Authorization": "Bearer " + settings.admin_token}, method="POST")
            with HTTP.open(request, timeout=10):
                pass
        print("编辑台后台服务已请求停止。")
        return
    if not health():
        log = settings.data_dir / "server.log"
        with log.open("ab") as output:
            process = subprocess.Popen([str(python), "-m", "newsroom.server"], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
        for _ in range(60):
            if health():
                break
            if process.poll() is not None:
                raise RuntimeError("服务启动失败，请查看 " + str(log))
            time.sleep(0.5)
        else:
            raise RuntimeError("启动超时，请查看 " + str(log))
    if args.run_now:
        request = urllib.request.Request(URL + "/api/run", headers={"Authorization": "Bearer " + settings.admin_token}, method="POST")
        with HTTP.open(request, timeout=10):
            pass
    print("编辑台：", URL)
    print("口令文件：", settings.data_dir / "admin-token.txt")
    print("后台运行中；关闭浏览器不影响每日采编。电脑需保持开机且不休眠。")
    if not args.no_browser:
        if not webbrowser.open(browser_login_url(settings.admin_token)):
            raise RuntimeError('默认浏览器未能打开，请设置可用的浏览器后重新双击启动')


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("启动失败：", error)
        raise SystemExit(1)
