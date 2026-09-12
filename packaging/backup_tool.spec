# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — 数据备份/还原工具 (onefile 单文件)。

为什么 onefile:
  这是个独立小工具, 需要与主程序 TickFlowStockPanel.exe 放在同一目录分发。
  用单文件可免去第二套 onedir 目录树 (几百 MB), 双击即用、方便拷贝。

构建 (在 backend/ 下执行, 产物与主程序同级):
  cd backend
  .venv\\Scripts\\python.exe -m PyInstaller --noconfirm ..\\packaging\\backup_tool.spec
产物: backend/dist/TickFlowDataBackup.exe
  (build-desktop.ps1 会自动把它复制进 dist/TickFlowStockPanel/ 与主程序同目录)
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
_IS_MACOS = sys.platform == "darwin"
APP_ICON = str(ROOT / "packaging" / ("icon.icns" if _IS_MACOS else "icon.ico"))

datas = []
binaries = []
hiddenimports = []

# pywebview 的平台后端 (winforms/gtk/cocoa) 是按字符串动态导入的, 静态分析抓不到。
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("webview.platforms")
# Windows 下 pywebview 走 pythonnet/clr, 同样需要显式收集。
hiddenimports += collect_submodules("clr_loader")
hiddenimports += collect_submodules("pythonnet")

# 版本探测 (importlib.metadata.version) 需要包元数据, 否则 frozen 后报
# PackageNotFoundError。容错写法: 包不存在就跳过。
for _pkg in ("pywebview", "pythonnet", "clr-loader"):
    try:
        datas += copy_metadata(_pkg)
    except Exception:
        pass

# 工具只用标准库 + pywebview, 把主程序那套重型依赖全部排除, 控制体积。
excludes = [
    "tkinter",
    "numpy", "pandas", "polars", "pyarrow", "duckdb", "fastexcel",
    "fastapi", "uvicorn", "starlette", "pydantic", "anyio", "httpx",
    "matplotlib", "plotly", "vectorbt", "numba", "llvmlite",
    "apscheduler", "openai", "jupyter", "IPython", "pytest",
]

a = Analysis(
    [str(ROOT / "packaging" / "backup_tool.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="TickFlowDataBackup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX 压缩原生库易崩, 与主程序一致关闭
    console=False,       # 窗口应用: 不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=APP_ICON,
)
