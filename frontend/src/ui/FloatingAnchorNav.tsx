import { useEffect, useMemo, useState } from 'react'

export interface AnchorItem {
  /** 目标区块的 DOM id（不含 #） */
  id: string
  /** 锚点显示文案 */
  label: string
}

/**
 * Krepling 特色右侧浮动锚点导航（frontend/DESIGN.md「锚点导航」）。
 * 竖排 pill dock，滚动时高亮当前区块；点击平滑跳转。
 * 选中态 = 浅紫底 #EDE9FE 高亮，不带左侧指示点/色条。
 */
export default function FloatingAnchorNav({ items }: { items: AnchorItem[] }) {
  const [active, setActive] = useState(items[0]?.id ?? '')
  const signature = useMemo(() => items.map(item => `${item.id}:${item.label}`).join('|'), [items])

  useEffect(() => {
    const onScroll = () => {
      let current = items[0]?.id ?? ''
      for (const item of items) {
        const el = document.getElementById(item.id)
        if (el && el.getBoundingClientRect().top <= 120) current = item.id
      }
      setActive(current)
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => window.removeEventListener('scroll', onScroll)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature])

  if (!items.length) return null
  return (
    <nav
      aria-label="页面锚点"
      style={{
        position: 'fixed',
        right: 20,
        top: '50%',
        transform: 'translateY(-50%)',
        zIndex: 900,
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
        padding: 6,
        background: 'var(--ac-card)',
        border: '1px solid var(--ac-line)',
        borderRadius: 'var(--ac-radius-pill)',
        boxShadow: 'var(--ac-shadow)',
      }}
    >
      {items.map(item => {
        const isActive = active === item.id
        return (
          <button
            key={item.id}
            type="button"
            aria-current={isActive ? 'true' : undefined}
            onClick={() => document.getElementById(item.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
            style={{
              border: 0,
              cursor: 'pointer',
              font: 'inherit',
              fontSize: 12,
              fontWeight: isActive ? 600 : 400,
              padding: '6px 14px',
              borderRadius: 'var(--ac-radius-pill)',
              background: isActive ? 'var(--ac-sel)' : 'transparent',
              color: isActive ? 'var(--ac-ink)' : 'var(--ac-muted)',
              whiteSpace: 'nowrap',
              transition: 'background var(--ac-t-micro) var(--ac-ease-out), color var(--ac-t-micro) var(--ac-ease-out)',
            }}
          >
            {item.label}
          </button>
        )
      })}
    </nav>
  )
}
