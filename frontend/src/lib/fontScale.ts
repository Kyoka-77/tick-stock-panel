// 界面字号缩放 — 全局 font-size 等比缩放
//
// 背景: 本仓库大量使用任意像素字号 (text-[9px] / text-[10px] / text-[11px]), 单纯改
// html 根字号 (rem) 覆盖不到它们, 会出现「一半字变大一半没变」。因此两类刻度都显式缩放:
//   - 标准刻度 (text-xs/sm/base/...) 与任意像素刻度 (text-[10px] 等)
//   - 只覆写 font-size, 不覆写 line-height (见 src/index.css 末尾的说明)
//
// 机制与主题保持一致: 状态存 localStorage('tf-font-scale'), 首屏由 index.html
// 内联脚本预设 CSS 变量, 避免刷新时字号跳变 (FOUC)。
import { useEffect, useState } from 'react'

const KEY = 'tf-font-scale'
const EVENT = 'tf-font-scale-change'

/** 允许范围 (超出会被夹紧): 低于 0.8 过小, 高于 1.5 密集表格会明显溢出 */
const MIN = 0.8
const MAX = 1.5

/** 可选的档位 (离散档位比滑块更好点, 也便于"一键还原") */
export const FONT_SCALE_LEVELS = [
  { value: 0.9, label: '小' },
  { value: 1, label: '标准' },
  { value: 1.1, label: '大' },
  { value: 1.2, label: '更大' },
  { value: 1.3, label: '特大' },
] as const

function clamp(v: number): number {
  if (!Number.isFinite(v)) return 1
  return Math.min(MAX, Math.max(MIN, v))
}

export function getFontScale(): number {
  try {
    const raw = localStorage.getItem(KEY)
    if (raw === null) return 1
    const v = parseFloat(raw)
    return Number.isFinite(v) ? clamp(v) : 1
  } catch {
    return 1
  }
}

/** 仅写 CSS 变量, 不落盘 (index.html 首屏脚本与运行时共用同一变量名) */
export function applyFontScale(scale: number): void {
  document.documentElement.style.setProperty('--tf-font-scale', String(clamp(scale)))
}

/** 落盘 + 立即生效 + 广播 (供同页其它组件与其它标签页同步) */
export function setFontScale(scale: number): number {
  const v = clamp(scale)
  try { localStorage.setItem(KEY, String(v)) } catch { /* 私隐模式等场景静默 */ }
  applyFontScale(v)
  window.dispatchEvent(new CustomEvent(EVENT, { detail: v }))
  return v
}

/** hook: 当前字号缩放系数 (1 = 标准) */
export function useFontScale(): number {
  const [scale, set] = useState<number>(getFontScale)
  useEffect(() => {
    const onChange = () => set(getFontScale())
    window.addEventListener(EVENT, onChange)
    window.addEventListener('storage', onChange) // 跨标签页同步
    return () => {
      window.removeEventListener(EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [])
  return scale
}

/** 档位标签 (找不到匹配档位时给出百分比描述) */
export function fontScaleLabel(scale: number): string {
  const hit = FONT_SCALE_LEVELS.find((l) => Math.abs(l.value - scale) < 0.001)
  return hit ? hit.label : `${Math.round(scale * 100)}%`
}
