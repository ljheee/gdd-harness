#!/usr/bin/env bash
# 静态检查（D1/D14）：Tauri 2 + WKWebView、无 Electron/Chromium、
# app 包内无控制台前端/Python/GDD 副本、ATS 仅放行本机网络、文档写明依赖。
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$ROOT/src-tauri/target/release/bundle/macos/GDD Console.app}"
CONF="$ROOT/src-tauri/tauri.conf.json"
FAILS=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }

if [ -d "$APP" ]; then
  pass "应用包存在：$APP"
else
  fail "应用包不存在：$APP（先执行 cargo tauri build --bundles app）"
fi

# 1. Tauri 配置：远程 frontendDist、无预置窗口、不暴露全局 Tauri API。
if python3 - "$CONF" <<'PY'
import json
import sys

conf = json.load(open(sys.argv[1], encoding="utf-8"))
frontend = conf.get("build", {}).get("frontendDist")
windows = conf.get("app", {}).get("windows")
global_tauri = conf.get("app", {}).get("withGlobalTauri")
print(f"frontendDist={frontend!r} windows={windows!r} withGlobalTauri={global_tauri!r}")
ok = frontend == "http://127.0.0.1:8765" and windows == [] and global_tauri is False
sys.exit(0 if ok else 1)
PY
then
  pass "tauri.conf.json：frontendDist 为控制台 URL、app.windows 为空、withGlobalTauri=false"
else
  fail "tauri.conf.json 不符合预期（见上）"
fi

if grep -q '^tauri = { version = "2"' "$ROOT/src-tauri/Cargo.toml"; then
  pass "依赖使用 Tauri 2"
else
  fail "src-tauri/Cargo.toml 未声明 tauri 2"
fi

# 2. 依赖树无 Electron/Chromium。
if [ -x "$HOME/.cargo/bin/cargo" ]; then
  CARGO="$HOME/.cargo/bin/cargo"
elif command -v cargo >/dev/null 2>&1; then
  CARGO="$(command -v cargo)"
else
  CARGO=""
fi
if [ -n "$CARGO" ]; then
  TREE="$("$CARGO" tree --manifest-path "$ROOT/src-tauri/Cargo.toml" --prefix none 2>/dev/null)"
  if printf '%s\n' "$TREE" | grep -Eiq 'electron|chromium'; then
    fail "依赖树出现 Electron/Chromium"
    printf '%s\n' "$TREE" | grep -Ei 'electron|chromium' | head -5
  else
    pass "依赖树无 Electron/Chromium"
  fi
else
  fail "cargo 不可用，无法检查依赖树"
fi

if [ ! -d "$APP" ]; then
  echo "汇总：$FAILS 项失败"
  exit 1
fi

# 3. app 包内无控制台页面/前端副本。
ASSETS="$(find "$APP/Contents" \( -name '*.html' -o -name '*.js' -o -name '*.css' \) -print 2>/dev/null)"
if [ -z "$ASSETS" ]; then
  pass "app 包内无 HTML/JS/CSS 资源副本"
else
  fail "app 包内发现前端资源："
  printf '%s\n' "$ASSETS"
fi

# 4. app 包内无 Python 运行时 / GDD 源码。
PY_ASSETS="$(find "$APP/Contents" \( -name 'Python.framework' -o -name 'python3*' -o -name '*.py' -o -path '*gdd/ui.py' \) -print 2>/dev/null)"
if [ -z "$PY_ASSETS" ]; then
  pass "app 包内无 Python 运行时/GDD 源码副本"
else
  fail "app 包内发现 Python/GDD 副本："
  printf '%s\n' "$PY_ASSETS"
fi

# 5. ATS：仅 NSAllowsLocalNetworking。
if plutil -p "$APP/Contents/Info.plist" 2>/dev/null | grep -q 'NSAllowsLocalNetworking'; then
  pass "Info.plist 含 NSAllowsLocalNetworking"
else
  fail "Info.plist 缺少 NSAllowsLocalNetworking"
fi

# 6. 文档要求本机 Python >=3.10 与 GDD 源码（D14）。
if grep -q '3\.10' "$ROOT/README.md" && grep -qi 'GDD 源码\|GDD source' "$ROOT/README.md"; then
  pass "README 说明本机 Python >=3.10 与 GDD 源码依赖"
else
  fail "README 未完整说明 Python >=3.10 / GDD 源码依赖"
fi
if grep -q '3\.10' "$ROOT/USER_GUIDE.md"; then
  pass "USER_GUIDE 说明 Python >=3.10"
else
  fail "USER_GUIDE 未说明 Python >=3.10"
fi

echo
if [ "$FAILS" -eq 0 ]; then
  echo "PASS: 静态检查全部通过"
  exit 0
fi
echo "FAIL: $FAILS 项未通过"
exit 1
