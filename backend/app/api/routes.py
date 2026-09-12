"""API 路由 — Phase 0 仅 /health 与 /api/capabilities。"""
from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app import __version__
from app.tickflow import client as tf_client
from app.tickflow.policy import detect_capabilities, tier_label

router = APIRouter()


# ── 导出内容转交系统浏览器 ───────────────────────────────────────────
# 桌面版是 WebView2, 前端 Blob / a.download 下载点了没有任何反应 (回测 CSV、
# 复盘 MD 等前端现算的导出都受影响)。统一方案: 前端把内容 POST 到这里落成
# 一次性临时文件, 再用桌面桥 open_external_url 把下载 URL 交给系统默认浏览器。
_EXPORT_HANDOFF_DIRNAME = ".export_handoff"
_EXPORT_HANDOFF_TTL_SECONDS = 3600


class ExportHandoffRequest(BaseModel):
    filename: str
    content: str
    media_type: str = "application/octet-stream"


def _export_handoff_root() -> Path:
    from app.config import settings

    return settings.data_dir / _EXPORT_HANDOFF_DIRNAME


def _safe_export_filename(name: str) -> str:
    """只保留基名并过滤非法字符, 杜绝目录穿越。"""
    base = Path(str(name).replace("\\", "/")).name.strip()
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '/\\:*?"<>|')
    base = base.lstrip(".") or "export"
    return base[:120]


def _content_disposition(filename: str) -> str:
    """构造 Content-Disposition 头值。

    HTTP 头值必须是 latin-1 可编码的 —— 直接把中文文件名写进 filename="..."
    会让响应头编码失败, 客户端只看到 500。因此: ASCII 回退名走 filename=,
    真实名按 RFC 5987 百分号编码走 filename*=UTF-8''。
    """
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "", ascii_name).lstrip(".") or "download"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


def _purge_export_handoff(root: Path) -> None:
    """清理过期的一次性导出 (尽力而为, 失败不影响主流程)。"""
    cutoff = time.time() - _EXPORT_HANDOFF_TTL_SECONDS
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


@router.post("/api/export/handoff")
def create_export_handoff(payload: ExportHandoffRequest) -> dict:
    """落一份一次性导出文件, 返回下载 URL (供系统浏览器下载)。"""
    root = _export_handoff_root()
    root.mkdir(parents=True, exist_ok=True)
    _purge_export_handoff(root)

    token = secrets.token_hex(16)
    safe_name = _safe_export_filename(payload.filename)
    target_dir = root / token
    target_dir.mkdir(parents=True, exist_ok=True)
    # 必须写 bytes: Path.write_text 在 Windows 下会做文本模式换行转换
    # (\n -> \r\n), 把内容里原有的 \r\n 变成 \r\r\n, 污染导出内容。
    (target_dir / safe_name).write_bytes(payload.content.encode("utf-8"))
    (target_dir / ".media_type").write_text(payload.media_type, encoding="utf-8")

    return {"url": f"/api/export/handoff/{token}/{quote(safe_name)}"}


@router.get("/api/export/handoff/{token}/{filename}")
def fetch_export_handoff(token: str, filename: str):
    """下载上面落的一次性文件 (带 Content-Disposition 附件头)。"""
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise HTTPException(status_code=404, detail="导出文件不存在或已过期")

    root = _export_handoff_root().resolve()
    target = (root / token / _safe_export_filename(filename)).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="导出文件不存在或已过期")

    media_file = target.parent / ".media_type"
    media_type = (
        media_file.read_text(encoding="utf-8").strip()
        if media_file.is_file()
        else "application/octet-stream"
    )
    safe_name = target.name
    return Response(
        content=target.read_bytes(),
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(safe_name),
            "Cache-Control": "no-store",
        },
    )


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        # 三态: none(无key/无效) / free(免费key) / api_key(付费档)
        "mode": tf_client.current_mode(),
    }


@router.get("/api/capabilities")
def capabilities() -> dict:
    """前端用来决定哪些功能可用、哪些灰显。"""
    capset = detect_capabilities()
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }


@router.post("/api/capabilities/redetect")
def redetect(request: Request) -> dict:
    """用户在设置页"重新检测"按钮。"""
    capset = detect_capabilities(force=True)
    # 同步刷新 app.state 快照 (minute_refresh 等服务的门控读这里) 与财务调度器,
    # 与 settings.py 各探测路径一致 — 否则重检测后服务侧仍读旧 capset 被错误门控
    request.app.state.capabilities = capset
    from app.api.settings import _sync_financial_scheduler_caps
    _sync_financial_scheduler_caps(request.app.state, capset)
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }
