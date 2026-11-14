import type { CSSProperties } from 'react'
import { t } from '../i18n'

export interface PipelineStep {
  label: string
}

/**
 * 制作流程步骤条（frontend/DESIGN.md「流程感」呈现）：
 * 素材导入 → 方案确认 → 下载/转写 → AI 选段 → 候选采纳 → 渲染成片 → 终审发布。
 * 已完成 = 近黑圆点 ✓；当前 = 紫色脉冲点 + 紫字；失败 = 红点；
 * 未开始 = 浅灰。选中态不用左侧色条（负责人硬性约束）。
 */
const STEPS: PipelineStep[] = [
  { label: t('素材导入') },
  { label: t('方案确认') },
  { label: t('下载 / 转写') },
  { label: t('AI 选段') },
  { label: t('候选采纳') },
  { label: t('渲染成片') },
  { label: t('终审发布') },
]

export default function PipelineSteps({ currentIndex, failed }: { currentIndex: number; failed?: boolean }) {
  const idx = Math.max(0, Math.min(STEPS.length - 1, currentIndex))
  return (
    <div
      role="list"
      aria-label={t('制作流程')}
      style={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: '6px 0',
        padding: '14px 18px',
        border: '1px solid var(--ac-line)',
        borderRadius: 'var(--ac-radius)',
        background: 'var(--ac-card)',
        boxShadow: 'var(--ac-shadow)',
        marginBottom: 24,
      }}
    >
      {STEPS.map((step, i) => {
        const done = i < idx && !failed
        const current = i === idx
        const isFailed = current && failed
        const dotStyle: CSSProperties = {
          width: 10,
          height: 10,
          borderRadius: '50%',
          flex: 'none',
          background: isFailed ? 'var(--ac-error)' : done ? 'var(--ac-ink)' : current ? 'var(--ac-accent)' : 'var(--ac-line)',
          boxShadow: current && !isFailed ? '0 0 0 4px color-mix(in srgb, var(--ac-accent) 18%, transparent)' : 'none',
        }
        const labelStyle: CSSProperties = {
          fontSize: 13,
          fontWeight: current ? 600 : 400,
          color: isFailed ? 'var(--ac-error)' : current ? 'var(--ac-accent)' : done ? 'var(--ac-ink)' : 'var(--ac-muted)',
          whiteSpace: 'nowrap',
        }
        return (
          <div key={step.label} role="listitem" style={{ display: 'flex', alignItems: 'center' }}>
            {i > 0 && (
              <span
                aria-hidden
                style={{ width: 22, height: 1, background: done ? 'var(--ac-line)' : 'var(--ac-line)', margin: '0 8px', display: 'inline-block' }}
              />
            )}
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '4px 6px' }}>
              <span style={dotStyle}>{done ? <span style={{ color: 'var(--ac-card)', fontSize: 8, lineHeight: 1, display: 'inline-block', transform: 'translateY(-1px)' }}>✓</span> : null}</span>
              <span style={labelStyle}>{step.label}</span>
            </span>
          </div>
        )
      })}
    </div>
  )
}
