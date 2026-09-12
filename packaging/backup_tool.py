"""TickFlow 数据备份 / 还原工具 (窗口版)

双击运行, 与主程序同目录。功能:
  * 按时间轴可视化展示历史备份 (时间 / 体积 / 文件数)
  * 一键新建备份 (robocopy 增量复制)
  * 选择任一历史备份还原 — 还原前自动对当前数据做安全快照
  * 删除备份 / 打开备份目录

数据目录约定 (与主程序一致):
  应用数据   <exe 所在目录>/data
  备份存放   %LOCALAPPDATA%/TickFlowStockPanel/data-backups/<时间戳>
             (与 build-desktop.ps1 完全一致, 两处共用同一份备份)

安全设计:
  * 还原是破坏性操作 → 执行前强制对当前 data 做一次 __pre-restore__ 快照
  * 主程序在运行时拒绝还原 (WebView2 占用 db/缓存文件), 并提示先关闭
  * robocopy exit code < 8 视为成功 (1 = 有文件复制, 属正常)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import webview

# 本工具同样以 console=False 打包 (GUI 子系统, 无控制台)。spawn 控制台子程序
# (robocopy / tasklist / taskkill / cmd) 时 Windows 会为其新建一个控制台窗口 ——
# 表现为删备份、还原、关主程序时闪黑窗。CREATE_NO_WINDOW 抑制之
# (非 Windows 恒为 0, 跨平台安全)。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ── 路径解析 ─────────────────────────────────────────────────────────
_IS_FROZEN = getattr(sys, "frozen", False)

if _IS_FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent
else:
    # 开发态: packaging/backup_tool.py -> 项目根
    APP_DIR = Path(__file__).resolve().parent.parent

# 应用数据目录 —— 必须与主程序 app/config.py 的 _user_data_root() 一致:
#   Windows: <exe 同级>/data
#   macOS:   .app 包内不可写(且替换 app 即丢数据), 走系统惯例目录
if _IS_FROZEN and sys.platform == "darwin":
    DATA_DIR = (
        Path.home() / "Library" / "Application Support" / "TickFlowStockPanel" / "data"
    )
else:
    DATA_DIR = APP_DIR / "data"

APP_EXE = APP_DIR / "TickFlowStockPanel.exe"
APP_PROC = "TickFlowStockPanel"


def _user_root() -> Path:
    """用户级根目录: Windows 用 %LOCALAPPDATA%, macOS 用 Application Support。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("LOCALAPPDATA") or Path.home())


# 与 build-desktop.ps1 保持一致的备份根目录
_BACKUP_ROOT_DEFAULT = _user_root() / "TickFlowStockPanel" / "data-backups"
CONFIG_PATH = _BACKUP_ROOT_DEFAULT.parent / "backup_tool.json"

PRE_RESTORE_PREFIX = "__pre-restore__"


# ── 工具函数 ─────────────────────────────────────────────────────────
def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def backup_root() -> Path:
    cfg = _load_config()
    custom = cfg.get("backup_root")
    if custom:
        try:
            return Path(custom)
        except Exception:  # noqa: BLE001
            pass
    return _BACKUP_ROOT_DEFAULT


def _dir_stat(path: Path) -> tuple[int, int]:
    """返回 (文件数, 总字节)。"""
    if not path.exists():
        return 0, 0
    files = 0
    total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += (Path(root) / n).stat().st_size
                files += 1
            except OSError:
                continue
    return files, total


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def _robocopy(src: Path, dst: Path) -> tuple[bool, str]:
    """robocopy 目录复制; exit code < 8 = 成功。"""
    dst.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["robocopy", str(src), str(dst), "/E", "/COPY:DAT", "/R:1", "/W:1",
             "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
            capture_output=True, text=True, timeout=3600,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "复制超时 (超过 1 小时)"
    except FileNotFoundError:
        return False, "找不到 robocopy (仅 Windows 支持)"
    code = proc.returncode
    if code >= 8:
        return False, f"robocopy 失败 (exit={code})"
    return True, f"exit={code}"


def _is_app_running() -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {APP_PROC}.exe", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        ).stdout
        return APP_PROC.lower() in out.lower()
    except Exception:  # noqa: BLE001
        return False


def _stop_app() -> bool:
    if os.name != "nt":
        return False
    try:
        subprocess.run(["taskkill", "/F", "/T", "/IM", f"{APP_PROC}.exe"],
                       capture_output=True, text=True, timeout=30,
                       creationflags=_NO_WINDOW)
        time.sleep(2.5)
        return not _is_app_running()
    except Exception:  # noqa: BLE001
        return False


def _open_path(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606
    except Exception:  # noqa: BLE001
        pass


# ── 业务逻辑 ─────────────────────────────────────────────────────────
def list_backups() -> list[dict]:
    root = backup_root()
    items: list[dict] = []
    if not root.exists():
        return items
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        files, size = _dir_stat(child)
        name = child.name
        is_safety = name.startswith(PRE_RESTORE_PREFIX)
        # 目录名是时间戳; 安全快照是 __pre-restore__<时间戳> 形式
        stamp = name.split(PRE_RESTORE_PREFIX, 1)[-1] if is_safety else name
        try:
            dt = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
            when = dt.strftime("%Y-%m-%d %H:%M:%S")
            ts = dt.timestamp()
        except ValueError:
            when = name
            ts = child.stat().st_mtime
        items.append({
            "name": name,
            "path": str(child),
            "time": when,
            "ts": ts,
            "size": size,
            "size_h": _human(size),
            "files": files,
            "safety": is_safety,
        })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


class Api:
    """暴露给前端的 JS API (window.pywebview.api.*)。"""

    def __init__(self) -> None:
        self._win = None
        self._busy = False
        self._lock = threading.Lock()

    def attach(self, win) -> None:
        self._win = win

    # -- 进度推送 ------------------------------------------------------
    def _push(self, message: str, percent: int | None = None, done: bool = False) -> None:
        if not self._win:
            return
        payload = {"message": message, "percent": percent, "done": done}
        try:
            self._win.evaluate_js(f"window.__onProgress({json.dumps(payload, ensure_ascii=False)})")
        except Exception:  # noqa: BLE001
            pass

    def _run(self, fn, *args) -> None:
        """长任务统一切到后台线程, 避免阻塞 UI 线程。"""
        with self._lock:
            if self._busy:
                self._push("已有任务在执行, 请稍候…")
                return
            self._busy = True

        def wrapper():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                self._push(f"执行失败: {e}")
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(target=wrapper, daemon=True).start()

    # -- 查询 ----------------------------------------------------------
    def bootstrap(self) -> dict:
        root = backup_root()
        data_files, data_size = _dir_stat(DATA_DIR)
        return {
            "app_dir": str(APP_DIR),
            "data_dir": str(DATA_DIR),
            "data_files": data_files,
            "data_size_h": _human(data_size),
            "backup_root": str(root),
            "backup_root_default": str(_BACKUP_ROOT_DEFAULT),
            "app_exe": str(APP_EXE),
            "app_running": _is_app_running(),
            "backups": list_backups(),
        }

    def refresh(self) -> dict:
        return self.bootstrap()

    # -- 备份 ----------------------------------------------------------
    def create_backup(self) -> None:
        self._run(self._create_backup)

    def _create_backup(self) -> None:
        if not DATA_DIR.exists():
            self._push("数据目录不存在, 无法备份", done=True)
            return
        if _is_app_running():
            self._push("检测到主程序正在运行…")
            if not _stop_app():
                self._push("无法关闭主程序, 请手动退出后重试", done=True)
                return
            self._push("主程序已关闭")

        files, size = _dir_stat(DATA_DIR)
        self._push(f"开始备份: {files} 个文件 / {_human(size)}", 2)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = backup_root() / stamp
        self._push(f"目标: {dst}", 5)
        ok, msg = _robocopy(DATA_DIR, dst)
        if not ok:
            self._push(f"备份失败: {msg}", done=True)
            return

        dfiles, dsize = _dir_stat(dst)
        if dfiles < files:
            self._push(f"备份校验异常 (源 {files} / 备份 {dfiles} 文件), 请检查磁盘空间", done=True)
            return
        self._push(f"备份完成: {dfiles} 个文件 / {_human(dsize)}", 100, done=True)

    # -- 还原 ----------------------------------------------------------
    def restore_backup(self, name: str) -> None:
        self._run(self._restore_backup, name)

    def _restore_backup(self, name: str) -> None:
        src = backup_root() / name
        if not src.is_dir():
            self._push(f"备份不存在: {name}", done=True)
            return
        if not DATA_DIR.parent.exists():
            self._push("应用目录不存在", done=True)
            return

        if _is_app_running():
            self._push("检测到主程序正在运行, 正在关闭…")
            if not _stop_app():
                self._push("无法关闭主程序, 请手动退出后重试", done=True)
                return
            self._push("主程序已关闭")

        # 1) 安全快照: 还原前先把当前数据存一份, 避免误操作不可逆
        if DATA_DIR.exists():
            cur_files, cur_size = _dir_stat(DATA_DIR)
            if cur_files > 0:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                snap = backup_root() / f"{PRE_RESTORE_PREFIX}{stamp}"
                self._push(f"还原前安全快照 ({cur_files} 文件 / {_human(cur_size)})…", 10)
                ok, msg = _robocopy(DATA_DIR, snap)
                if not ok:
                    self._push(f"安全快照失败, 已中止还原 (未改动任何数据): {msg}", done=True)
                    return
                self._push("安全快照完成", 25)

        # 2) 清空并还原
        self._push("清空当前数据目录…", 35)
        try:
            for child in DATA_DIR.iterdir():
                if child.is_dir():
                    subprocess.run(["cmd", "/c", "rmdir", "/S", "/Q", str(child)],
                                   capture_output=True, timeout=600,
                                   creationflags=_NO_WINDOW)
                else:
                    child.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            self._push(f"清空失败: {e}", done=True)
            return

        self._push(f"还原中: {src.name}", 50)
        ok, msg = _robocopy(src, DATA_DIR)
        if not ok:
            self._push(f"还原失败: {msg} (安全快照已保留, 可再次还原)", done=True)
            return

        files, size = _dir_stat(DATA_DIR)
        self._push(f"还原完成: {files} 个文件 / {_human(size)}", 100, done=True)

    # -- 删除 ----------------------------------------------------------
    def delete_backup(self, name: str) -> dict:
        target = backup_root() / name
        if not target.is_dir():
            return {"ok": False, "message": "备份不存在"}
        try:
            subprocess.run(["cmd", "/c", "rmdir", "/S", "/Q", str(target)],
                           capture_output=True, timeout=600,
                           creationflags=_NO_WINDOW)
            return {"ok": True, "message": f"已删除 {name}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"删除失败: {e}"}

    # -- 杂项 ----------------------------------------------------------
    def open_backup_dir(self) -> None:
        _open_path(backup_root())

    def open_data_dir(self) -> None:
        _open_path(DATA_DIR)

    def set_backup_root(self, path: str) -> dict:
        p = (path or "").strip()
        if not p:
            return {"ok": False, "message": "路径不能为空"}
        try:
            newp = Path(p)
            newp.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"目录不可用: {e}"}
        cfg = _load_config()
        cfg["backup_root"] = str(newp)
        _save_config(cfg)
        return {"ok": True, "message": f"备份目录已改为 {newp}"}

    def reset_backup_root(self) -> dict:
        cfg = _load_config()
        cfg.pop("backup_root", None)
        _save_config(cfg)
        return {"ok": True, "message": "已恢复默认备份目录"}

    def stop_app(self) -> dict:
        if not _is_app_running():
            return {"ok": True, "message": "主程序未在运行"}
        ok = _stop_app()
        return {"ok": ok, "message": "已关闭主程序" if ok else "关闭失败, 请手动退出"}


# ── 前端页面 (内嵌, 免外部资源) ───────────────────────────────────────
HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>TickFlow 数据备份 / 还原</title>
<style>
  :root {
    --base:#0A0A0B; --surface:#18181B; --elevated:#212126; --border:#353539;
    --fg:#FAFAFA; --fg2:#C4C4CB; --muted:#8E8E96; --accent:#3B82F6;
    --ok:#12B76A; --warn:#F79009; --danger:#F04438;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body {
    background:var(--base); color:var(--fg); height:100vh; overflow:hidden;
    font-family:'Inter','HarmonyOS Sans SC','PingFang SC',system-ui,sans-serif;
    font-size:13px; display:flex; flex-direction:column;
  }
  header { padding:14px 18px; border-bottom:1px solid var(--border); flex-shrink:0; }
  h1 { font-size:15px; font-weight:600; display:flex; align-items:center; gap:8px; }
  .dot { width:8px; height:8px; border-radius:50%; background:var(--accent); }
  .paths { margin-top:7px; display:grid; gap:3px; font-size:11px; color:var(--muted); }
  .paths b { color:var(--fg2); font-weight:500; }
  .paths .row { display:flex; gap:6px; align-items:center; }
  .link { color:var(--accent); cursor:pointer; text-decoration:underline; background:none; border:none; font-size:11px; padding:0; }
  .bar { display:flex; gap:8px; align-items:center; padding:10px 18px; border-bottom:1px solid var(--border); flex-shrink:0; flex-wrap:wrap; }
  button {
    font-family:inherit; font-size:12px; cursor:pointer; border-radius:6px;
    border:1px solid var(--border); background:var(--elevated); color:var(--fg2);
    padding:6px 12px; transition:.15s;
  }
  button:hover:not(:disabled) { border-color:var(--accent); color:var(--fg); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.primary { background:rgba(59,130,246,.15); border-color:rgba(59,130,246,.5); color:#93C5FD; }
  button.primary:hover:not(:disabled) { background:rgba(59,130,246,.25); }
  .status { margin-left:auto; font-size:11px; color:var(--muted); }
  .status.run { color:var(--warn); }
  main { flex:1; overflow-y:auto; padding:14px 18px 20px; }
  .chart-wrap { border:1px solid var(--border); border-radius:8px; background:var(--surface); padding:12px 14px; }
  .chart-title { font-size:12px; color:var(--fg2); margin-bottom:10px; display:flex; justify-content:space-between; }
  .chart { display:flex; align-items:flex-end; gap:4px; height:110px; overflow-x:auto; padding-bottom:4px; }
  .cbar { flex:0 0 26px; display:flex; flex-direction:column; justify-content:flex-end; align-items:center; gap:3px; cursor:pointer; }
  .cbar .box { width:100%; border-radius:3px 3px 0 0; background:linear-gradient(180deg,#3B82F6,#1D4ED8); min-height:3px; transition:.15s; }
  .cbar.safety .box { background:linear-gradient(180deg,#F79009,#B45309); }
  .cbar:hover .box { filter:brightness(1.35); }
  .cbar .lbl { font-size:9px; color:var(--muted); white-space:nowrap; }
  .empty { color:var(--muted); font-size:12px; padding:22px; text-align:center; }
  table { width:100%; border-collapse:collapse; margin-top:14px; font-size:12px; }
  th { text-align:left; color:var(--muted); font-weight:500; font-size:11px; padding:6px 8px; border-bottom:1px solid var(--border); }
  td { padding:7px 8px; border-bottom:1px solid rgba(53,53,57,.5); color:var(--fg2); }
  tr:last-child td { border-bottom:none; }
  tr.row-safety td:first-child::before { content:'安全快照 '; color:var(--warn); font-size:10px; }
  .acts { display:flex; gap:6px; justify-content:flex-end; }
  .acts button { padding:3px 9px; font-size:11px; }
  .danger { border-color:rgba(240,68,56,.4); color:#FCA5A5; }
  .danger:hover:not(:disabled) { background:rgba(240,68,56,.15); border-color:var(--danger); }
  .restore { border-color:rgba(18,183,106,.4); color:#6EE7B7; }
  .restore:hover:not(:disabled) { background:rgba(18,183,106,.15); border-color:var(--ok); }
  footer { border-top:1px solid var(--border); padding:9px 18px; flex-shrink:0; }
  .prog { height:5px; background:var(--elevated); border-radius:99px; overflow:hidden; }
  .prog i { display:block; height:100%; width:0; background:linear-gradient(90deg,#3B82F6,#8B5CF6); transition:width .25s; }
  .prog-txt { font-size:11px; color:var(--muted); margin-top:6px; min-height:14px; }
  .mask { position:fixed; inset:0; background:rgba(0,0,0,.62); display:none; align-items:center; justify-content:center; z-index:50; }
  .mask.on { display:flex; }
  .dlg { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:20px; width:400px; }
  .dlg h2 { font-size:14px; margin-bottom:9px; }
  .dlg p { font-size:12px; color:var(--fg2); line-height:1.6; }
  .dlg .warnbox { margin-top:10px; padding:9px 11px; border-radius:6px; background:rgba(240,68,56,.1); border:1px solid rgba(240,68,56,.3); color:#FCA5A5; font-size:11px; line-height:1.6; }
  .dlg .btns { display:flex; gap:8px; justify-content:flex-end; margin-top:16px; }
  .banner { margin-top:8px; padding:6px 10px; border-radius:6px; font-size:11px; background:rgba(247,144,9,.12); border:1px solid rgba(247,144,9,.35); color:#FCD34D; }
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span>TickFlow 数据备份 / 还原</h1>
  <div class="paths">
    <div class="row"><b>数据目录</b><span id="p-data">-</span><button class="link" onclick="api.open_data_dir()">打开</button></div>
    <div class="row"><b>备份目录</b><span id="p-backup">-</span><button class="link" onclick="api.open_backup_dir()">打开</button></div>
    <div class="row"><b>当前数据</b><span id="p-stat">-</span></div>
  </div>
  <div id="running-banner" class="banner" style="display:none">
    主程序正在运行 —— 还原前会自动关闭它 (避免文件占用)。若不想被关闭, 请先手动退出。
    <button class="link" onclick="stopApp()">立即关闭主程序</button>
  </div>
</header>

<div class="bar">
  <button id="btn-backup" class="primary" onclick="createBackup()">＋ 新建备份</button>
  <button onclick="refresh()">刷新</button>
  <button onclick="changeRoot()">更改备份目录</button>
  <span class="status" id="status">就绪</span>
</div>

<main>
  <div class="chart-wrap">
    <div class="chart-title"><span>备份时间轴（柱高 = 体积）</span><span id="chart-legend"></span></div>
    <div class="chart" id="chart"></div>
  </div>
  <table>
    <thead><tr><th>备份时间</th><th>体积</th><th>文件数</th><th style="text-align:right">操作</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</main>

<footer>
  <div class="prog"><i id="progbar"></i></div>
  <div class="prog-txt" id="progtxt"></div>
</footer>

<div class="mask" id="mask">
  <div class="dlg">
    <h2 id="dlg-title">确认</h2>
    <p id="dlg-body"></p>
    <div class="warnbox" id="dlg-warn" style="display:none"></div>
    <div class="btns">
      <button onclick="closeDlg()">取消</button>
      <button class="primary" id="dlg-ok">确认</button>
    </div>
  </div>
</div>

<script>
let BACKUPS = [];
let api = null;

function fmtSize(n) {
  const u = ['B','KB','MB','GB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + u[i];
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function setStatus(text, running) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = 'status' + (running ? ' run' : '');
}

window.__onProgress = (p) => {
  if (p.percent != null) document.getElementById('progbar').style.width = p.percent + '%';
  if (p.message) document.getElementById('progtxt').textContent = p.message;
  if (p.done) {
    setStatus('就绪', false);
    document.getElementById('btn-backup').disabled = false;
    setTimeout(() => { document.getElementById('progbar').style.width = '0%'; }, 900);
    refresh();
  }
};

function render(data) {
  document.getElementById('p-data').textContent = data.data_dir;
  document.getElementById('p-backup').textContent = data.backup_root;
  document.getElementById('p-stat').textContent = data.data_files + ' 个文件 · ' + data.data_size_h;
  document.getElementById('running-banner').style.display = data.app_running ? 'block' : 'none';
  BACKUPS = data.backups || [];
  renderChart();
  renderTable();
}

function renderChart() {
  const chart = document.getElementById('chart');
  const legend = document.getElementById('chart-legend');
  if (!BACKUPS.length) {
    chart.innerHTML = '<div class="empty">暂无备份 —— 点击「新建备份」开始</div>';
    legend.textContent = '';
    return;
  }
  const max = Math.max(...BACKUPS.map(b => b.size), 1);
  // 时间轴: 旧 → 新 从左到右
  const asc = [...BACKUPS].sort((a, b) => a.ts - b.ts);
  chart.innerHTML = asc.map(b => {
    const h = Math.max(4, Math.round(b.size / max * 96));
    const d = new Date(b.ts * 1000);
    const lbl = (d.getMonth() + 1) + '/' + d.getDate();
    const title = `${b.time}\n${b.size_h} · ${b.files} 文件${b.safety ? ' (安全快照)' : ''}`;
    return `<div class="cbar${b.safety ? ' safety' : ''}" title="${esc(title)}" onclick="confirmRestore('${esc(b.name)}')">
      <div class="lbl">${esc(b.size_h.replace(' ', ''))}</div>
      <div class="box" style="height:${h}px"></div>
      <div class="lbl">${lbl}</div>
    </div>`;
  }).join('');
  legend.textContent = asc.length + ' 份 · 最大 ' + fmtSize(max);
}

function renderTable() {
  const tb = document.getElementById('tbody');
  if (!BACKUPS.length) {
    tb.innerHTML = '<tr><td colspan="4"><div class="empty">暂无备份</div></td></tr>';
    return;
  }
  tb.innerHTML = BACKUPS.map(b => `
    <tr class="${b.safety ? 'row-safety' : ''}">
      <td>${esc(b.time)}</td>
      <td>${esc(b.size_h)}</td>
      <td>${b.files}</td>
      <td><div class="acts">
        <button class="restore" onclick="confirmRestore('${esc(b.name)}')">还原</button>
        <button class="danger" onclick="confirmDelete('${esc(b.name)}')">删除</button>
      </div></td>
    </tr>`).join('');
}

// ── 对话框 ─────────────────────────────────────────────────────────
function openDlg(title, body, warn, onOk) {
  document.getElementById('dlg-title').textContent = title;
  document.getElementById('dlg-body').innerHTML = body;
  const w = document.getElementById('dlg-warn');
  if (warn) { w.style.display = 'block'; w.innerHTML = warn; } else { w.style.display = 'none'; }
  const ok = document.getElementById('dlg-ok');
  ok.onclick = () => { closeDlg(); onOk(); };
  document.getElementById('mask').classList.add('on');
}
function closeDlg() { document.getElementById('mask').classList.remove('on'); }

function confirmRestore(name) {
  const b = BACKUPS.find(x => x.name === name);
  const info = b ? `${esc(b.time)} · ${esc(b.size_h)} · ${b.files} 个文件` : esc(name);
  openDlg('确认还原', 
    `将用该备份<b>完全覆盖</b>当前数据目录：<br><br>${info}`,
    `⚠️ 还原是破坏性操作：当前 <b>data/</b> 会被清空后替换。<br>为避免误操作，程序会先自动做一份 <b>安全快照</b>，之后仍可还原回来。<br>主程序若在运行会被自动关闭。`,
    () => doRestore(name));
}

function confirmDelete(name) {
  openDlg('确认删除', `将永久删除备份：<br><br>${esc(name)}`, '', () => doDelete(name));
}

function doRestore(name) {
  setStatus('还原中…', true);
  document.getElementById('btn-backup').disabled = true;
  api.restore_backup(name);
}
function doDelete(name) {
  setStatus('删除中…', true);
  api.delete_backup(name).then(r => { setStatus(r.message, false); refresh(); });
}

function createBackup() {
  setStatus('备份中…', true);
  document.getElementById('btn-backup').disabled = true;
  api.create_backup();
}
function refresh() {
  api.refresh().then(d => { render(d); setStatus('就绪', false); });
}
function stopApp() { api.stop_app().then(r => { setStatus(r.message, false); refresh(); }); }
function changeRoot() {
  const cur = document.getElementById('p-backup').textContent;
  const p = prompt('输入新的备份目录（留空取消）', cur);
  if (!p) return;
  api.set_backup_root(p).then(r => { setStatus(r.message, false); refresh(); });
}

window.addEventListener('pywebviewready', () => {
  api = window.pywebview.api;
  refresh();
});
</script>
</body>
</html>
"""


def main() -> None:
    api = Api()
    win = webview.create_window(
        "TickFlow 数据备份 / 还原",
        html=HTML,
        width=1040,
        height=760,
        min_size=(880, 620),
        js_api=api,
    )
    api.attach(win)
    # 与主程序一致: 持久化 WebView2 配置, 且不复用主程序的 profile (各自独立)
    storage = _BACKUP_ROOT_DEFAULT.parent / "backup-tool-webview"
    try:
        storage.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        storage = None
    webview.start(private_mode=False, storage_path=str(storage) if storage else None)


if __name__ == "__main__":
    main()
