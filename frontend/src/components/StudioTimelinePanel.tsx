// 审核台时间轴面板（R-2/R-3）：源视频预览 + 信号曲线 + 词刻度 + 逐词切点拖拽 + 快捷键。
// 深色剪辑台：与亮色 AntD 工作台形成功能分区（NLE 语境），等宽字体时间码。
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Space, Tag, message } from 'antd'
import { StudioCandidate, fmtMs, studioApi } from '../services/studioApi'

interface Word { text: string; start_ms: number; end_ms: number }
type Tracks = Record<string, { bucket_ms: number; series: Record<string, number[]> }>

interface Props {
  materialId: string | null
  durationMs: number
  words: Word[]
  signals: Tracks | null
  waveform: number[] | null
  clips0: StudioCandidate[]
  clips1: StudioCandidate[]
  onChanged: () => void
}

const H = 168
const COLORS: Record<string, string> = { danmaku: '#22d3ee', loudness: '#f59e0b' }
const tc = (ms: number) => {
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}.${String(Math.floor((ms % 1000) / 10)).padStart(2, '0')}`
}
const weak = (c: StudioCandidate) => !!(c.scores && (c.scores as Record<string, unknown>).not_recommended)

export default function StudioTimelinePanel({
  materialId, durationMs, words, signals, waveform, clips0, clips1, onChanged,
}: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [selected, setSelected] = useState<StudioCandidate | null>(null)
  const [draft, setDraft] = useState<{ start_ms: number; end_ms: number } | null>(null)
  const [currentMs, setCurrentMs] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [drag, setDrag] = useState<'start' | 'end' | null>(null)
  const [hover, setHover] = useState<'start' | 'end' | null>(null)
  const dragRef = useRef(drag)
  dragRef.current = drag

  const dur = durationMs || 1
  const selStart = draft?.start_ms ?? selected?.start_ms ?? 0
  const selEnd = draft?.end_ms ?? selected?.end_ms ?? 0

  const xToMs = useCallback((x: number) => {
    const w = canvasRef.current?.clientWidth || 1
    return Math.max(0, Math.min(dur, (x / w) * dur))
  }, [dur])
  const msToX = useCallback((ms: number) => {
    const w = canvasRef.current?.clientWidth || 1
    return (ms / dur) * w
  }, [dur])

  const snap = useCallback((ms: number, edge: 'start' | 'end') => {
    if (!words.length) return ms
    let best = ms
    let bestD = 1000 // 超过 1s 不吸附，避免远跳
    for (const w of words) {
      const v = edge === 'start' ? w.start_ms : w.end_ms
      const d = Math.abs(v - ms)
      if (d < bestD) { bestD = d; best = v }
    }
    return best
  }, [words])

  const selectClip = useCallback((c: StudioCandidate | null) => {
    setSelected(c)
    setDraft(null)
    if (c && videoRef.current) videoRef.current.currentTime = c.start_ms / 1000
  }, [])

  // 播放头平滑刷新
  useEffect(() => {
    if (!playing) return
    let raf = 0
    const tick = () => {
      if (videoRef.current) setCurrentMs(videoRef.current.currentTime * 1000)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [playing])

  // 绘制
  useEffect(() => {
    const cv = canvasRef.current
    const wrap = wrapRef.current
    if (!cv || !wrap) return
    const w = wrap.clientWidth
    const dpr = window.devicePixelRatio || 1
    cv.width = w * dpr
    cv.height = H * dpr
    cv.style.width = `${w}px`
    cv.style.height = `${H}px`
    const ctx = cv.getContext('2d')
    if (!ctx) return
    ctx.scale(dpr, dpr)
    ctx.fillStyle = '#14161c'
    ctx.fillRect(0, 0, w, H)

    // 时间网格
    ctx.font = '10px ui-monospace, Consolas, monospace'
    for (let i = 0; i <= 10; i++) {
      const x = (w * i) / 10
      ctx.strokeStyle = '#23262e'
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke()
      ctx.fillStyle = '#6b7280'
      if (i < 10) ctx.fillText(fmtMs((dur * i) / 10), x + 3, 12)
    }

    // 音频振幅波形（W-2：16..52 lane，选中区内高亮）
    if (waveform && waveform.length) {
      const n = waveform.length
      const bw = Math.max(1, w / n - 0.4)
      for (let i = 0; i < n; i++) {
        const ms = ((i + 0.5) / n) * dur
        const inSel = selected && ms >= selStart && ms < selEnd
        const x = (i / n) * w
        const h = Math.max(0.5, waveform[i] * 18)
        ctx.fillStyle = inSel ? 'rgba(56,189,248,0.55)' : '#2f3542'
        ctx.fillRect(x, 34 - h, bw, h * 2)
      }
    }

    // 信号曲线（弹幕青/响度橙，归一化，66..90 lane）
    if (signals) {
      let si = 0
      for (const [, track] of Object.entries(signals)) {
        for (const [name, series] of Object.entries(track.series)) {
          if (!series.length) continue
          const max = Math.max(...series, 1e-6)
          ctx.strokeStyle = COLORS[name] || '#9ca3af'
          ctx.lineWidth = 1.4
          ctx.beginPath()
          series.forEach((v, i) => {
            const x = ((i + 0.5) / series.length) * w
            const y = 90 - (v / max) * 24
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
          })
          ctx.stroke()
          ctx.fillStyle = COLORS[name] || '#9ca3af'
          ctx.fillText(name, 4 + si * 70, 62)
          si++
        }
      }
    }

    // 词刻度（96..104）
    for (const wd of words) {
      const x = msToX(wd.start_ms)
      const inSel = selected && wd.start_ms >= selStart && wd.start_ms < selEnd
      ctx.strokeStyle = inSel ? '#9ca3af' : '#3a3f4b'
      ctx.beginPath(); ctx.moveTo(x, 96); ctx.lineTo(x, 104); ctx.stroke()
    }

    // clips0 候选区域（108..138）
    for (const c of clips0) {
      const x1 = msToX(c.start_ms)
      const x2 = msToX(c.end_ms)
      const sel = selected?.id === c.id
      ctx.fillStyle = sel ? 'rgba(56,189,248,0.22)' : 'rgba(59,130,246,0.14)'
      ctx.fillRect(x1, 108, x2 - x1, 30)
      ctx.strokeStyle = sel ? '#38bdf8' : 'rgba(59,130,246,0.45)'
      ctx.strokeRect(x1, 108, x2 - x1, 30)
    }
    // clips1 已采纳绿线（142）
    for (const c of clips1) {
      if (!c.adopted) continue
      ctx.fillStyle = '#34d399'
      ctx.fillRect(msToX(c.start_ms), 142, msToX(c.end_ms) - msToX(c.start_ms), 3)
    }

    // 选中区手柄
    if (selected) {
      const x1 = msToX(selStart)
      const x2 = msToX(selEnd)
      ctx.fillStyle = 'rgba(56,189,248,0.10)'
      ctx.fillRect(x1, 108, x2 - x1, 30)
      for (const [hx, edge] of [[x1, 'start'], [x2, 'end']] as [number, 'start' | 'end'][]) {
        const active = drag === edge || hover === edge
        ctx.fillStyle = active ? '#38bdf8' : '#e5e7eb'
        const hw = active ? 7 : 5
        ctx.fillRect(hx - hw / 2, 104, hw, 38)
        ctx.fillStyle = '#14161c'
        ctx.fillRect(hx - 1, 116, 2, 14)
      }
      ctx.fillStyle = '#e5e7eb'
      ctx.font = '10px ui-monospace, Consolas, monospace'
      ctx.fillText(`${tc(selStart)} → ${tc(selEnd)}`, Math.min(x1 + 4, w - 130), 158)
    }

    // 播放头
    const px = msToX(currentMs)
    ctx.strokeStyle = '#ef4444'
    ctx.lineWidth = 1.5
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke()
    ctx.fillStyle = '#ef4444'
    ctx.beginPath(); ctx.moveTo(px - 5, 0); ctx.lineTo(px + 5, 0); ctx.lineTo(px, 7); ctx.closePath(); ctx.fill()
  }, [signals, waveform, words, clips0, clips1, selected, draft, currentMs, drag, hover, dur, msToX, selStart, selEnd])

  // 拖拽（window 级监听，出画布不丢事件）
  useEffect(() => {
    if (!drag) return
    const move = (e: MouseEvent) => {
      const rect = canvasRef.current?.getBoundingClientRect()
      if (!rect) return
      const ms = xToMs(e.clientX - rect.left)
      setDraft((prev) => {
        const s = prev?.start_ms ?? selected?.start_ms ?? 0
        const en = prev?.end_ms ?? selected?.end_ms ?? 0
        return drag === 'start'
          ? { start_ms: Math.min(ms, en - 100), end_ms: en }
          : { start_ms: s, end_ms: Math.max(ms, s + 100) }
      })
    }
    const up = () => {
      setDrag(null)
      setDraft((prev) => {
        if (!prev || !selected) return prev
        const s = snap(prev.start_ms, 'start')
        const en = snap(prev.end_ms, 'end')
        if (selected.layer === 'clips1') {
          studioApi.patchClips1(selected.id, { user: 'reviewer', start_ms: s, end_ms: en })
            .then(() => { message.success('切点已精修（吸附词界）'); onChanged() })
            .catch((e) => message.error(String(e)))
          return null
        }
        message.info('切点已调整（吸附词界），点「采纳」生效')
        return { start_ms: s, end_ms: en }
      })
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
  }, [drag, selected, snap, xToMs, onChanged])

  // 快捷键：空格播放/I 入点/O 出点/[ ] 切换候选
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      if (e.code === 'Space') {
        e.preventDefault()
        const v = videoRef.current
        if (v) { if (v.paused) void v.play(); else v.pause() }
      } else if (e.key === 'i' || e.key === 'I') {
        if (!selected) return
        const s = snap(currentMs, 'start')
        setDraft((p) => ({ start_ms: s, end_ms: p?.end_ms ?? selected.end_ms }))
      } else if (e.key === 'o' || e.key === 'O') {
        if (!selected) return
        const en = snap(currentMs, 'end')
        setDraft((p) => ({ start_ms: p?.start_ms ?? selected.start_ms, end_ms: en }))
      } else if (e.key === '[' || e.key === ']') {
        if (!clips0.length) return
        const idx = clips0.findIndex((c) => c.id === selected?.id)
        const next = e.key === ']'
          ? clips0[(idx + 1) % clips0.length]
          : clips0[(idx - 1 + clips0.length) % clips0.length]
        selectClip(next)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selected, currentMs, clips0, snap, selectClip])

  const hitHandle = (x: number): 'start' | 'end' | null => {
    if (!selected) return null
    if (Math.abs(x - msToX(selStart)) <= 7) return 'start'
    if (Math.abs(x - msToX(selEnd)) <= 7) return 'end'
    return null
  }

  const doAdopt = async () => {
    if (!selected) return
    try {
      await studioApi.adopt(selected.id, {
        user: 'reviewer', start_ms: selStart, end_ms: selEnd, title: selected.title ?? undefined,
      })
      message.success('已采纳为 clips1')
      selectClip(null)
      onChanged()
    } catch (e) { message.error(String(e)) }
  }

  return (
    <div ref={wrapRef} style={{
      background: '#0f1115', border: '1px solid #262a33', borderRadius: 8,
      padding: 12, marginBottom: 12,
    }}>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 10 }}>
        <video
          ref={videoRef}
          src={materialId ? studioApi.materialFileUrl(materialId) : undefined}
          style={{ width: 300, maxHeight: 180, background: '#000', borderRadius: 6 }}
          onTimeUpdate={(e) => { if (!playing) setCurrentMs(e.currentTarget.currentTime * 1000) }}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          controls
        />
        <div style={{ flex: 1, minWidth: 240, color: '#d1d5db' }}>
          {selected ? (
            <>
              <Space size={6} wrap style={{ marginBottom: 6 }}>
                <Tag color={selected.layer === 'clips1' ? 'green' : 'blue'}>{selected.layer}</Tag>
                <Tag>{selected.clip_type || '未分类'}</Tag>
                <span style={{ fontFamily: 'ui-monospace, Consolas, monospace', color: '#38bdf8' }}>
                  {selected.rank_score ?? '-'} 分
                </span>
                {weak(selected) && <Tag color="orange">不建议发布</Tag>}
              </Space>
              <div style={{ fontSize: 13, marginBottom: 4 }}>{selected.title || '（无标题）'}</div>
              <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 8 }}>{selected.reason}</div>
              <Space wrap>
                {selected.layer === 'clips0' && (
                  <Button size="small" type="primary" onClick={doAdopt}>
                    采纳{draft ? '（用调整后切点）' : ''}
                  </Button>
                )}
                <span style={{ fontSize: 12, color: '#6b7280', fontFamily: 'ui-monospace, Consolas, monospace' }}>
                  {tc(currentMs)} / {tc(dur)}
                </span>
              </Space>
            </>
          ) : (
            <div style={{ fontSize: 13, color: '#6b7280', paddingTop: 8 }}>
              点击时间轴上的候选区域选中，或按 [ / ] 切换
            </div>
          )}
          <div style={{ fontSize: 11, color: '#4b5563', marginTop: 10, lineHeight: 1.8 }}>
            快捷键：<b>空格</b> 播放/暂停 · <b>I</b> 入点 · <b>O</b> 出点 · <b>[ ]</b> 切换候选<br />
            拖拽白色手柄调切点（松手自动吸附词界）；clips1 直接落库，clips0 采纳时生效
          </div>
        </div>
      </div>
      <canvas
        ref={canvasRef}
        style={{ display: 'block', cursor: drag ? 'ew-resize' : hover ? 'ew-resize' : 'crosshair', borderRadius: 4 }}
        onMouseDown={(e) => {
          const rect = e.currentTarget.getBoundingClientRect()
          const x = e.clientX - rect.left
          const h = hitHandle(x)
          if (h) { setDrag(h); return }
          // 点击候选区域选中；空白处 seek
          const ms = xToMs(x)
          const hit = clips0.find((c) => ms >= c.start_ms && ms <= c.end_ms)
          if (hit) selectClip(hit)
          else if (videoRef.current) videoRef.current.currentTime = ms / 1000
        }}
        onMouseMove={(e) => {
          if (dragRef.current) return
          const rect = e.currentTarget.getBoundingClientRect()
          setHover(hitHandle(e.clientX - rect.left))
        }}
        onMouseLeave={() => setHover(null)}
      />
      <div style={{ display: 'flex', gap: 14, marginTop: 6, fontSize: 11, color: '#6b7280' }}>
        <span><span style={{ color: '#2f3542' }}>▮</span> 音频波形</span>
        <span><span style={{ color: '#22d3ee' }}>━</span> 弹幕密度</span>
        <span><span style={{ color: '#f59e0b' }}>━</span> 响度</span>
        <span><span style={{ color: '#3b82f6' }}>▮</span> AI 候选</span>
        <span><span style={{ color: '#34d399' }}>▬</span> 已采纳</span>
        <span><span style={{ color: '#ef4444' }}>│</span> 播放头</span>
      </div>
    </div>
  )
}