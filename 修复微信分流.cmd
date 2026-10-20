@echo off
chcp 65001 >nul
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先双击“一键启动.cmd”准备运行环境。
) else (
  ".venv\Scripts\python.exe" "scripts\repair_wechat_routing.py" --apply
)
echo.
echo 此修复让微信直连，不固定宽带公网 IP，也不修改微信白名单。
pause
popd
