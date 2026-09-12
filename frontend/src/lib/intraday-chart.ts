import type { MinuteKlineRow } from '@/lib/api'

/** 从 datetime 串取 HH:MM。契约: 分钟K datetime 已在后端入口统一为北京墙钟, 前端不做时区换算。 */
export function formatMinuteTime(datetime: string): string {
  const match = datetime.match(/(\d{2}):(\d{2})/)
  if (!match) return datetime.slice(11, 16)
  return `${match[1]}:${match[2]}`
}

export function computeIntradayAverage(data: MinuteKlineRow[]): (number | null)[] {
  const result: (number | null)[] = []
  let amount = 0
  let volume = 0
  let hasAmount = true
  for (const row of data) {
    if (typeof row.amount === 'number' && Number.isFinite(row.amount)) {
      amount += row.amount
    } else {
      hasAmount = false
    }
    volume += row.volume * 100
    result.push(hasAmount && volume > 0 ? amount / volume : null)
  }
  return result
}

/**
 * A 股一个交易日的分钟时间刻度 (北京时间)。
 *
 * 口径必须与上游分钟数据严格一致 —— 每天 241 个点:
 *   上午  09:30 … 11:30   共 121 点 (09:30 为开盘集合竞价那一根)
 *   下午  13:01 … 15:00   共 120 点 (分钟数据以「分钟结束时刻」标注, **没有 13:00**)
 *
 * ⚠️ 下午若从 13:00 起算, 会凭空多出一个永远填不上数据的 13:00 空槽,
 *    使网格 242 槽 ≠ 数据 241 点 —— 表现为整个下午段相对上午错位一格(差 1 分钟),
 *    且日期标签锚点也会落在空槽上。此常量被单日图与 5 日图共用, 改动需同步两处图表。
 */
function generateFullDayTimes(): string[] {
  const times: string[] = []
  // 上午: 09:30(开盘竞价) + 09:31..11:30
  for (let hour = 9; hour <= 11; hour++) {
    const startMinute = hour === 9 ? 30 : 0
    const endMinute = hour === 11 ? 30 : 59
    for (let minute = startMinute; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  // 下午: 13:01..15:00 (无 13:00)
  for (let hour = 13; hour <= 15; hour++) {
    const startMinute = hour === 13 ? 1 : 0
    const endMinute = hour === 15 ? 0 : 59
    for (let minute = startMinute; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  return times
}

export const FULL_DAY_TIMES = generateFullDayTimes()
