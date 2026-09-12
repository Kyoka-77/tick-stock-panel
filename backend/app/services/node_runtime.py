"""定位 Node.js / npm —— 处理 macOS GUI 启动时 PATH 不含常见安装位置的问题。

背景 (为什么需要这个模块):
    macOS 双击 .app 启动时, 进程由 launchd 拉起, PATH 是最小集合
    (/usr/bin:/bin:/usr/sbin:/sbin 一类), **不会加载 ~/.zshrc / ~/.zprofile**。
    于是 Homebrew (/opt/homebrew/bin)、nvm、volta、fnm、MacPorts 以及 Node 官方
    pkg (/usr/local/bin) 安装的 node / npm 统统不在 PATH 里 —— 表现为
    「终端里 node -v 正常, 但应用内提示未找到 node / 未找到 npm」。

    本模块在**进程内**把这些常见位置补进 os.environ['PATH'] (不动系统配置、
    不写任何 rc 文件), 幂等且可重复调用。仅 macOS 生效, 其它平台保持原状。

    也支持显式覆盖:
      STOCK_SDK_NODE  指向 node 可执行文件
      STOCK_SDK_NPM   指向 npm 可执行文件
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# macOS 常见 Node 安装位置 (Apple Silicon 优先)
_MAC_FIXED_DIRS = (
    "/opt/homebrew/bin",          # Homebrew (Apple Silicon)
    "/opt/homebrew/opt/node/bin",  # Homebrew node formula
    "/usr/local/bin",             # Homebrew (Intel) / Node 官方 pkg
    "/usr/local/opt/node/bin",
    "/opt/local/bin",             # MacPorts
)

# 版本管理器: 相对 $HOME 的 glob (多版本时取字典序最大的, 通常是最新)
_MAC_GLOB_DIRS = (
    ".volta/bin",
    ".nvm/versions/node/*/bin",
    ".fnm/node-versions/*/installation/bin",
    ".local/share/fnm/node-versions/*/installation/bin",
    "Library/pnpm",               # pnpm 独立安装 (自带 node 软链)
    ".bun/bin",
)

_PATCHED = False


def candidate_dirs() -> list[Path]:
    """返回候选 Node 目录 (存在性未过滤)。"""
    dirs: list[Path] = [Path(p) for p in _MAC_FIXED_DIRS]
    home = Path.home()
    for pattern in _MAC_GLOB_DIRS:
        try:
            dirs.extend(sorted(home.glob(pattern), reverse=True))
        except OSError:
            continue
    return dirs


def ensure_node_on_path() -> list[str]:
    """把常见 Node 安装目录**追加**到本进程 PATH。返回实际新增的目录。

    追加(而非前置)以免覆盖系统自带的同名工具; 幂等, 可重复调用。
    """
    global _PATCHED
    if sys.platform != "darwin":
        return []
    if _PATCHED:
        return []

    current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    known = set(current)
    added: list[str] = []
    for directory in candidate_dirs():
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        text = str(directory)
        if text in known:
            continue
        added.append(text)
        known.add(text)

    if added:
        os.environ["PATH"] = os.pathsep.join([*current, *added])
    _PATCHED = True
    return added


def _resolve(explicit: str | None, exe: str) -> str | None:
    # 先补全 PATH (仅 macOS 生效), 否则 GUI 启动时永远找不到 Homebrew/nvm 装的 node。
    ensure_node_on_path()
    if explicit:
        return explicit if (Path(explicit).exists() or shutil.which(explicit)) else None
    return shutil.which(exe)


def find_node() -> str | None:
    """定位 node: 优先 STOCK_SDK_NODE, 否则补全 PATH 后查找。"""
    return _resolve(os.getenv("STOCK_SDK_NODE"), "node")


def find_npm() -> str | None:
    """定位 npm: 优先 STOCK_SDK_NPM, 否则补全 PATH 后查找。"""
    return _resolve(os.getenv("STOCK_SDK_NPM"), "npm")
