#!/usr/bin/env bash
# ============================================================================
#  TickFlow 桌面版构建 — macOS (Apple Silicon / arm64)
#
#  为什么单独一个脚本:
#    PyInstaller 不支持交叉编译 —— Mach-O 二进制与 .app 只能在 macOS 上生成。
#    Windows 侧的 build-desktop.ps1 无法产出 macOS 产物, 因此这里给出对等的
#    macOS 构建流程, 产物落到独立目录 backend/dist-macos/ (与 Windows 的
#    backend/dist/ 完全隔离, 互不覆盖)。
#
#  用法:
#    chmod +x build-desktop-macos.sh
#    ./build-desktop-macos.sh                 # 构建 .app
#    ./build-desktop-macos.sh --dmg           # 额外产出 .dmg
#    ./build-desktop-macos.sh --skip-frontend # 只重打后端 (前端 dist 已就绪时)
#
#  产物:
#    backend/dist-macos/TickFlowStockPanel.app
#    backend/dist-macos/TickFlowStockPanel-<version>-arm64.dmg   (--dmg)
#
#  前置: Xcode Command Line Tools (xcode-select --install)、uv、pnpm、Node 18+
#
#  注: 本脚本在 Windows 上无法执行 (也无需执行) —— 请在 macOS 上运行。
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
PACKAGING_DIR="$ROOT/packaging"
OUT_DIR="$BACKEND_DIR/dist-macos"
WORK_DIR="$BACKEND_DIR/build-macos"
APP_NAME="TickFlowStockPanel"
APP_PATH="$OUT_DIR/$APP_NAME.app"
# 打包态数据目录: config.py 的 _user_data_root() = exe 同级 data/
# (.app 内即 Contents/MacOS/data)
APP_DATA_DIR="$APP_PATH/Contents/MacOS/data"
BACKUP_ROOT="${HOME}/Library/Application Support/TickFlowStockPanel/build-backups"

DO_DMG=0
SKIP_FRONTEND=0
for arg in "$@"; do
  case "$arg" in
    --dmg) DO_DMG=1 ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg (可用: --dmg / --skip-frontend)"; exit 2 ;;
  esac
done

log()  { printf '\033[90m[build]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[build]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[build]\033[0m %s\n' "$*"; exit 1; }

echo "=================================================="
echo "  TickFlow 桌面版构建 (macOS)"
echo "=================================================="
echo "    项目根   : $ROOT"
echo "    产物目录 : $OUT_DIR"
echo "    数据目录 : $APP_DATA_DIR"
echo "    构建备份 : $BACKUP_ROOT"
echo ""

# ── 0. 环境检查 ───────────────────────────────────────────────────────────
log "0/7  环境检查"
[ "$(uname -s)" = "Darwin" ] || die "本脚本只能在 macOS 上运行 (当前: $(uname -s))"
ARCH="$(uname -m)"
if [ "$ARCH" != "arm64" ]; then
  warn "当前架构是 $ARCH (Intel)。"
  warn "官方只支持 Apple Silicon (arm64): 自 polars 1.33 起已移除 macOS x86_64 原生 wheel,"
  warn "Intel 上极可能装不上依赖。继续构建可能失败。"
fi
command -v uv   >/dev/null 2>&1 || die "未找到 uv  (安装: brew install uv)"
command -v pnpm >/dev/null 2>&1 || die "未找到 pnpm (安装: npm i -g pnpm)"
command -v node >/dev/null 2>&1 || die "未找到 node (需 Node.js >= 18)"
[ -f "$PACKAGING_DIR/tickflow.spec" ] || die "找不到 $PACKAGING_DIR/tickflow.spec"
ok "uv / pnpm / node / spec 就位  (arch=$ARCH, node=$(node -v))"

# ── 1. 关闭运行中的实例 ───────────────────────────────────────────────────
log "1/7  关闭运行中的实例"
if pgrep -f "$APP_NAME" >/dev/null 2>&1; then
  warn "检测到 $APP_NAME 正在运行, 正在关闭…"
  pkill -f "$APP_NAME" || true
  sleep 2
fi
ok "无运行中实例 (或已关闭)"

# ── 2. 备份现有用户数据 ───────────────────────────────────────────────────
# PyInstaller 重建 .app 会整体替换 bundle, 其中的 data/ 会一并消失。
# 与 Windows 侧 build-desktop.ps1 同样的「先备份、后还原」策略。
log "2/7  备份用户数据"
STAMP="$(date +%Y%m%d-%H%M%S)"
SNAPSHOT=""
if [ -d "$APP_DATA_DIR" ] && [ -n "$(ls -A "$APP_DATA_DIR" 2>/dev/null || true)" ]; then
  mkdir -p "$BACKUP_ROOT"
  SNAPSHOT="$BACKUP_ROOT/$STAMP"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$APP_DATA_DIR/" "$SNAPSHOT/"
  else
    cp -R "$APP_DATA_DIR" "$SNAPSHOT"
  fi
  ok "已备份到 $SNAPSHOT  ($(du -sh "$SNAPSHOT" | cut -f1))"
else
  log "无既有数据, 跳过备份"
fi

# ── 3. 构建前端 ───────────────────────────────────────────────────────────
log "3/7  构建前端"
if [ "$SKIP_FRONTEND" -eq 1 ]; then
  [ -f "$FRONTEND_DIR/dist/index.html" ] || die "--skip-frontend 但 frontend/dist 不存在"
  log "已按 --skip-frontend 跳过"
else
  ( cd "$FRONTEND_DIR" && pnpm build )
  [ -f "$FRONTEND_DIR/dist/index.html" ] || die "前端构建产物缺失"
fi
ok "前端已输出到 frontend/dist"

# ── 4. 生成 macOS 图标 (.icns) ────────────────────────────────────────────
# spec 在 macOS 上读 packaging/icon.icns; 该文件未入库, 必须现场生成,
# 否则 BUNDLE 会因找不到图标而失败。
log "4/7  生成 macOS 图标"
if [ -f "$PACKAGING_DIR/icon.icns" ]; then
  ok "icon.icns 已存在, 跳过"
else
  ( cd "$BACKEND_DIR" && uv run python "$PACKAGING_DIR/generate_icon.py" )
  [ -f "$PACKAGING_DIR/icon.icns" ] || die "icon.icns 生成失败"
  ok "已生成 icon.icns"
fi

# ── 5. PyInstaller 打包 ───────────────────────────────────────────────────
# 与 release.yml 一致: 显式 --distpath/--workpath, 产物直达独立目录。
# 注意不要用 `uv run pyinstaller` 的默认行为在别处落产物; 这里路径全部写死。
log "5/7  PyInstaller 打包 (.app)"
( cd "$BACKEND_DIR" && uv sync --extra desktop --frozen )
( cd "$BACKEND_DIR" && uv pip install --python .venv/bin/python pyinstaller )

rm -rf "$OUT_DIR" "$WORK_DIR"
mkdir -p "$OUT_DIR"

( cd "$BACKEND_DIR" && uv run python -m PyInstaller \
    --noconfirm \
    --distpath "$OUT_DIR" \
    --workpath "$WORK_DIR" \
    "$PACKAGING_DIR/tickflow.spec" )

[ -d "$APP_PATH" ] || die "未产出 $APP_PATH"
ok "已生成 $APP_PATH"

# ── 6. 还原用户数据 + ad-hoc 签名 ─────────────────────────────────────────
log "6/7  还原用户数据 / 签名"
if [ -n "$SNAPSHOT" ] && [ -d "$SNAPSHOT" ]; then
  mkdir -p "$APP_DATA_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "$SNAPSHOT/" "$APP_DATA_DIR/"
  else
    cp -R "$SNAPSHOT/." "$APP_DATA_DIR/"
  fi
  ok "已还原用户数据 ($(find "$APP_DATA_DIR" -type f | wc -l | tr -d ' ') 个文件)"
else
  log "无需还原数据"
fi

# Apple Silicon 上可执行文件必须至少有 ad-hoc 签名, 否则内核直接拒绝执行。
# 分发版本应改用 Developer ID: codesign --sign "Developer ID Application: ..."
log "对 .app 做 ad-hoc 签名 (arm64 必需)"
codesign --force --deep --sign - "$APP_PATH" \
  || die "codesign 失败 (可先执行: xcode-select --install)"
codesign --verify --verbose=2 "$APP_PATH" >/dev/null 2>&1 \
  && ok "签名校验通过" \
  || warn "签名校验未通过, 但通常仍可运行"
xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

# ── 7. 可选: 打包 .dmg ────────────────────────────────────────────────────
log "7/7  收尾"
VERSION="$(cd "$FRONTEND_DIR" && node -p "require('./package.json').version" 2>/dev/null || echo 0.0.0)"
if [ "$DO_DMG" -eq 1 ]; then
  DMG="$OUT_DIR/TickFlowStockPanel-$VERSION-arm64.dmg"
  rm -f "$DMG"
  STAGE="$(mktemp -d)"
  cp -R "$APP_PATH" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -volname "TickFlow 股票面板" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
  rm -rf "$STAGE"
  ok "已生成 $DMG"
fi

echo ""
echo "=================================================="
echo "  构建成功"
echo "=================================================="
echo "    应用     : $APP_PATH"
echo "    体积     : $(du -sh "$APP_PATH" | cut -f1)"
echo "    架构     : $ARCH"
echo "    构建备份 : ${SNAPSHOT:-（无）}"
echo ""
echo "  首次运行: Finder 里右键 > 打开 (未签名会提示「无法验证开发者」,"
echo "            因为这里只做了 ad-hoc 签名; 之后即可正常双击)。"
echo ""
