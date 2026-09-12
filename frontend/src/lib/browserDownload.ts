/**
 * 把前端生成的内容交给「系统默认浏览器」下载。
 *
 * 为什么需要它: 桌面版跑在 WebView2 里, 前端常见的 Blob + a.download 下载点
 * 下去没有任何反应 (Blob 下载被静默丢弃)。回测 CSV、复盘 MD 这类「前端现算
 * 内容」的导出都受影响。
 *
 * 统一方案 (与「下载策略」一致):
 *   1. 内容 POST 给后端 /api/export/handoff, 落成一次性临时文件;
 *   2. 拿到下载 URL, 用桌面桥 open_external_url 交给系统默认浏览器;
 *   3. 普通浏览器环境直接 window.open 该 URL。
 *   任何一步失败都退回原生 Blob 下载, 保证浏览器场景不退化。
 */

interface DesktopBridge {
  pywebview?: {
    api?: { open_external_url?: (url: string) => Promise<boolean> }
  }
}

export async function downloadInBrowser(
  filename: string,
  content: string,
  mimeType = 'application/octet-stream',
): Promise<void> {
  const bridge = (window as unknown as DesktopBridge).pywebview

  try {
    const res = await fetch('/api/export/handoff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, content, media_type: mimeType }),
    })
    if (!res.ok) throw new Error(`handoff failed: ${res.status}`)
    const data = (await res.json()) as { url?: string }
    if (!data?.url) throw new Error('handoff url missing')

    const abs = new URL(data.url, window.location.origin).toString()

    // 桌面版: 交给系统默认浏览器 (WebView2 自身不做下载)
    if (bridge?.api?.open_external_url) {
      await bridge.api.open_external_url(abs)
      return
    }
    if (window.open(abs, '_blank', 'noopener')) return
    throw new Error('window.open blocked')
  } catch {
    // 兜底: 原生 Blob 下载 (普通浏览器可用; 桌面版会静默失败但不影响其它功能)
    const blob = new Blob([content], { type: mimeType })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }
}
