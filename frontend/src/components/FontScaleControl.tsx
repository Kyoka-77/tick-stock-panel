/**
 * 界面字号控件。
 *
 * variant="icon"   — 侧边栏用: 图标按钮 + 上浮档位菜单 (与主题切换按钮同排)
 * variant="inline" — 设置页用: 一行档位按钮
 */
import { useEffect, useRef, useState } from 'react'
import { Type } from 'lucide-react'
import { cn } from '@/lib/cn'
import {
  FONT_SCALE_LEVELS,
  fontScaleLabel,
  setFontScale,
  useFontScale,
} from '@/lib/fontScale'

function isActive(level: number, scale: number): boolean {
  return Math.abs(level - scale) < 0.001
}

/** 设置页: 一排档位按钮 */
function InlineLevels() {
  const scale = useFontScale()
  return (
    <div className="flex flex-wrap items-center gap-1">
      {FONT_SCALE_LEVELS.map((lv) => (
        <button
          key={lv.value}
          type="button"
          onClick={() => setFontScale(lv.value)}
          className={cn(
            'h-8 rounded-btn border px-3 text-xs transition-colors duration-150 ease-smooth cursor-pointer',
            isActive(lv.value, scale)
              ? 'border-accent/40 bg-accent/10 font-medium text-accent'
              : 'border-border bg-base text-secondary hover:border-accent/30 hover:text-foreground',
          )}
        >
          {lv.label}
        </button>
      ))}
    </div>
  )
}

/** 侧栏: 图标按钮 + 上浮菜单 */
function IconPicker() {
  const scale = useFontScale()
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)

  // 点击外部 / Esc 关闭
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={`界面字号: ${fontScaleLabel(scale)} (${Math.round(scale * 100)}%)`}
        className={cn(
          'flex items-center justify-center rounded-btn p-2 transition-colors duration-150 ease-smooth cursor-pointer',
          open ? 'bg-elevated text-foreground' : 'text-foreground/80 hover:bg-elevated hover:text-foreground',
        )}
      >
        <Type className="h-4 w-4 shrink-0" />
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1 w-36 rounded-card border border-border bg-surface p-1 shadow-lg">
          {FONT_SCALE_LEVELS.map((lv) => (
            <button
              key={lv.value}
              type="button"
              onClick={() => {
                setFontScale(lv.value)
                setOpen(false)
              }}
              className={cn(
                'flex w-full items-center justify-between rounded-btn px-2 py-1.5 text-xs transition-colors duration-150 ease-smooth cursor-pointer',
                isActive(lv.value, scale)
                  ? 'bg-accent/10 text-accent'
                  : 'text-secondary hover:bg-elevated hover:text-foreground',
              )}
            >
              <span>{lv.label}</span>
              <span className="text-[10px] opacity-70">{Math.round(lv.value * 100)}%</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export function FontScaleControl({ variant = 'icon' }: { variant?: 'icon' | 'inline' }) {
  return variant === 'inline' ? <InlineLevels /> : <IconPicker />
}
