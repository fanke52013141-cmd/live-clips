// 成片审阅台 + 渲染/钩子工坊（前端批 F-3/F-4）。
// 视频预览 + 平台安全区遮罩 + 剪辑/钩子开关 + L1/L2 文案编辑 + 数字进度轮询。
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Button, Card, Checkbox, Collapse, Descriptions, Input, InputNumber, Modal, Select,
  Space, Switch, Table, Tag, message,
} from 'antd'
import {
  studioApi, fmtMs,
  type StudioArtifact, type StudioCandidate, type StudioProject,
  type PlatformPreset, type RenderOptions, type StudioTask, type HookEstimate,
} from '../services/studioApi'

const DEFAULT_OPTS: RenderOptions = {
  platform: 'douyin', portrait: true, subtitles: true,
  debreath: false, filler_cut: false, keep_breath_ms: 250,
  cold_open: true, cold_open_jumpcut: false, flash_forward: false,
  l1_overlay: true, l2_tts: false, l2_rank_threshold: 80,
  use_gen_hooks: false,
}

export default function StudioRenderPage() {
  const [projects, setProjects] = useState<StudioProject[]>([])
  const [pid, setPid] = useState<string>('')
  const [opts, setOpts] = useState<RenderOptions>(DEFAULT_OPTS)
  const [task, setTask] = useState<StudioTask | null>(null)
  const [artifacts, setArtifacts] = useState<StudioArtifact[]>([])
  const [clips1, setClips1] = useState<StudioCandidate[]>([])
  const [presets, setPresets] = useState<Record<string, PlatformPreset>>({})
  const [selected, setSelected] = useState<StudioArtifact | null>(null)
  const [maskPlatform, setMaskPlatform] = useState<string>('douyin')
  const [showMask, setShowMask] = useState(true)
  const [busy, setBusy] = useState(false)
  // 生成素材工坊（P3 前端增强）
  const [genLevel, setGenLevel] = useState<3 | 4>(3)
  const [genCloud, setGenCloud] = useState(false)
  const [estimate, setEstimate] = useState<HookEstimate | null>(null)
  const [genBusy, setGenBusy] = useState(false)
  const [selectedGenIds, setSelectedGenIds] = useState<string[]>([])
  const timer = useRef<number | null>(null)

  useEffect(() => {
    studioApi.projects().then(setProjects).catch((e) => message.error(String(e)))
    studioApi.platforms().then(setPresets).catch(() => undefined)
    return () => { if (timer.current) window.clearInterval(timer.current) }
  }, [])

  const loadResults = useCallback(async (project: string) => {
    if (!project) return
    try {
      const [arts, c1] = await Promise.all([
        studioApi.artifacts(project),
        studioApi.candidates(project, 'clips1'),
      ])
      setArtifacts(arts)
      setClips1(c1.filter((c) => c.adopted))
      const mp4 = arts.find((a) => a.kind === 'mp4')
      setSelected(mp4 || null)
    } catch (e) { message.error(String(e)) }
  }, [])

  const onProject = (v: string) => {
    setPid(v)
    setTask(null)
    loadResults(v)
    studioApi.task(v).then(setTask).catch(() => undefined)
  }

  const poll = useCallback((project: string) => {
    if (timer.current) window.clearInterval(timer.current)
    timer.current = window.setInterval(async () => {
      try {
        const t = await studioApi.task(project)
        setTask(t)
        if (t && t.kind === 'render' && ['done', 'failed', 'cancelled'].includes(t.status)) {
          if (timer.current) window.clearInterval(timer.current)
          setBusy(false)
          if (t.status === 'done') { message.success('渲染完成'); loadResults(project) }
          else if (t.status === 'failed') message.error(t.error || '渲染失败')
        }
      } catch { /* 轮询失败静默重试 */ }
    }, 1500)
  }, [loadResults])

  const doRender = async () => {
    if (!pid) return
    setBusy(true)
    try {
      await studioApi.render(pid, {
        ...opts,
        gen_asset_ids: opts.use_gen_hooks && selectedGenIds.length > 0 ? selectedGenIds : undefined,
      })
      poll(pid)
    } catch (e) { setBusy(false); message.error(String(e)) }
  }

  const genProvider = `${genCloud ? 'cloud' : 'mock'}_${genLevel === 3 ? 'image' : 'video'}`

  const doEstimate = async () => {
    if (!pid) return
    setGenBusy(true)
    try { setEstimate(await studioApi.hookEstimate(pid, { level: genLevel, provider: genProvider })) }
    catch (e) { message.error(String(e)) }
    finally { setGenBusy(false) }
  }

  const doGenerate = () => {
    if (!pid || !estimate) return
    Modal.confirm({
      title: '生成费用确认',
      content: (
        <div>
          <p>{estimate.gate}，共 {estimate.items.length} 条候选。</p>
          <p>预估费用：<b>{estimate.total} {estimate.currency}</b>{estimate.total === 0 ? '（mock 零费用）' : ''}</p>
          <p style={{ color: '#999', fontSize: 12 }}>{estimate.note}</p>
        </div>
      ),
      okText: '确认生成',
      cancelText: '取消',
      onOk: async () => {
        try {
          const r = await studioApi.hookGenerate(pid, {
            level: genLevel, provider: genProvider, confirm: true,
          })
          message.success(`生成完成：${r.generated.length} 条${r.skipped.length ? `，跳过 ${r.skipped.length} 条` : ''}`)
          setEstimate(null)
          loadResults(pid)
        } catch (e) { message.error(String(e)) }
      },
    })
  }

  const toggleGenSelect = (id: string, checked: boolean) => {
    setSelectedGenIds((prev) => checked ? [...prev, id] : prev.filter((x) => x !== id))
  }

  const saveHookText = async (c: StudioCandidate, level: 'l1_text' | 'l2_tts', text: string) => {
    const hp = { ...(c.hook_plan || {}), [level]: { ...((c.hook_plan as any)?.[level] || {}), text } }
    try {
      await studioApi.patchClips1(c.id, { user: 'ui', hook_plan: hp })
      message.success('钩子文案已保存')
      loadResults(pid)
    } catch (e) { message.error(String(e)) }
  }

  const preset = presets[maskPlatform]
  const mp4s = artifacts.filter((a) => a.kind === 'mp4')
  const hookArts = artifacts.filter((a) => a.kind === 'hook_image' || a.kind === 'hook_video')
  const coverOf = (cid: string | null) => artifacts.find((a) => a.kind === 'cover' && a.candidate_id === cid)
  const renderTask = task && task.kind === 'render' ? task : null
  const prog = renderTask?.progress

  return (
    <div style={{ padding: 16, display: 'grid', gap: 16 }}>
      <Card size="small" title="渲染 / 钩子工坊">
        <Space wrap>
          <Select
            style={{ width: 260 }} placeholder="选择项目" value={pid || undefined}
            onChange={onProject}
            options={projects.map((p) => ({ value: p.id, label: `${p.name}（${p.status}）` }))}
          />
          <Select
            style={{ width: 120 }} value={opts.platform}
            onChange={(v) => setOpts({ ...opts, platform: v })}
            options={Object.keys(presets).map((k) => ({ value: k, label: presets[k].name }))}
          />
          <Button type="primary" loading={busy} disabled={!pid} onClick={doRender}>开始渲染</Button>
          <Button disabled={!busy} onClick={() => studioApi.cancel(pid).then(() => message.info('已请求取消'))}>取消</Button>
        </Space>
        <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 8 }}>
          {([
            ['portrait', '竖屏 9:16'], ['subtitles', '逐词字幕'], ['debreath', '去气口（三重判定）'],
            ['filler_cut', '口头禅剪除（保守）'], ['cold_open', '冷开场（原生钩子）'],
            ['cold_open_jumpcut', '冷开场跳剪原位'], ['flash_forward', '高能闪现+whoosh'],
            ['l1_overlay', 'L1 文字贴片'], ['l2_tts', 'L2 TTS 配音'],
            ['use_gen_hooks', '接入生成素材（L3/L4）'],
          ] as [keyof RenderOptions, string][]).map(([k, label]) => (
            <Space key={k}>
              <Switch
                size="small" checked={Boolean(opts[k])}
                onChange={(v) => setOpts({ ...opts, [k]: v })}
              />
              <span>{label}</span>
            </Space>
          ))}
          <Space>
            <span>L2 rank 门槛</span>
            <InputNumber
              size="small" min={0} max={100} value={opts.l2_rank_threshold}
              onChange={(v) => setOpts({ ...opts, l2_rank_threshold: v ?? 80 })}
            />
          </Space>
        </div>
        {renderTask && (
          <div style={{ marginTop: 12 }}>
            <Tag color={renderTask.status === 'done' ? 'green' : renderTask.status === 'failed' ? 'red' : 'blue'}>
              {renderTask.status}
            </Tag>
            {prog && <span>{prog.message}：{prog.done}/{prog.total} {prog.unit}</span>}
            {renderTask.error && <Alert type="error" message={renderTask.error} style={{ marginTop: 8 }} />}
            {prog?.degraded?.map((d, i) => (
              <Alert key={i} type="warning" showIcon style={{ marginTop: 4 }}
                message={`降级 ${d.engine}：${d.reason}`} />
            ))}
          </div>
        )}
      </Card>

      <Card size="small" title="钩子文案（人工编辑，AI 只出候选）">
        <Table
          size="small" rowKey="id" pagination={false}
          dataSource={clips1}
          columns={[
            { title: '区间', render: (_, c) => `${fmtMs(c.start_ms)}–${fmtMs(c.end_ms)}` },
            { title: '标题', dataIndex: 'title', ellipsis: true },
            {
              title: 'L1 悬念句（≤20字）', render: (_, c) => (
                <Input
                  size="small" defaultValue={(c.hook_plan as any)?.l1_text?.text || ''}
                  placeholder="留空=用标题回退"
                  onBlur={(e) => saveHookText(c, 'l1_text', e.target.value)}
                />
              ),
            },
            {
              title: 'L2 配音文本', render: (_, c) => (
                <Input
                  size="small" defaultValue={(c.hook_plan as any)?.l2_tts?.text || ''}
                  placeholder="留空=用 L1/标题"
                  onBlur={(e) => saveHookText(c, 'l2_tts', e.target.value)}
                />
              ),
            },
          ]}
        />
      </Card>

      <Card size="small" title="生成素材工坊（L3 生图 / L4 生视频）">
        <Space wrap>
          <Select
            style={{ width: 150 }} value={genLevel}
            onChange={(v) => { setGenLevel(v); setEstimate(null) }}
            options={[{ value: 3, label: 'L3 生图 B-roll' }, { value: 4, label: 'L4 生视频冷开场' }]}
          />
          <Select
            style={{ width: 200 }} value={genCloud ? 'cloud' : 'mock'}
            onChange={(v) => { setGenCloud(v === 'cloud'); setEstimate(null) }}
            options={[
              { value: 'mock', label: 'mock（零费用开发）' },
              { value: 'cloud', label: 'cloud（需配置+付费开关）' },
            ]}
          />
          <Button disabled={!pid} loading={genBusy} onClick={doEstimate}>预估费用</Button>
          <Button type="primary" disabled={!pid || !estimate} onClick={doGenerate}>
            生成（需确认）
          </Button>
          {estimate && (
            <span>
              预估 <b>{estimate.total} {estimate.currency}</b>
              {estimate.total === 0 && <Tag color="green" style={{ marginLeft: 4 }}>零费用</Tag>}
              <span style={{ color: '#999', marginLeft: 8, fontSize: 12 }}>{estimate.gate}</span>
            </span>
          )}
        </Space>
        {estimate && (
          <Alert type="info" style={{ marginTop: 8 }} message={estimate.note} />
        )}
        <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 12 }}>
          {hookArts.map((a) => {
            const isMock = Boolean(a.meta?.mock)
            const isVideo = a.kind === 'hook_video'
            return (
              <div key={a.id} style={{ border: '1px solid #e8e8e8', borderRadius: 6, padding: 8 }}>
                <Space size={4} style={{ marginBottom: 6 }}>
                  <Tag color={isVideo ? 'purple' : 'blue'}>{isVideo ? 'L4 生视频' : 'L3 生图'}</Tag>
                  {isMock
                    ? <Tag color="orange">MOCK 占位 · 禁入真实渲染</Tag>
                    : <Tag color="green">真实素材</Tag>}
                </Space>
                {isMock ? (
                  <div style={{
                    height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    background: '#fafafa', border: '1px dashed #d9d9d9', borderRadius: 4,
                    color: '#999', fontSize: 12, padding: 8, textAlign: 'center',
                  }}>
                    mock 占位产物（非真实媒体）<br />{String(a.meta?.prompt || '').slice(0, 40)}
                  </div>
                ) : isVideo ? (
                  <video controls style={{ width: '100%', borderRadius: 4, background: '#000' }}
                    src={studioApi.artifactFileUrl(a.id)} />
                ) : (
                  <img alt="L3 生图" style={{ width: '100%', borderRadius: 4 }}
                    src={studioApi.artifactFileUrl(a.id)} />
                )}
                <div style={{ marginTop: 6, fontSize: 12, color: '#666' }}>
                  候选 {a.candidate_id?.slice(0, 8)}
                </div>
                <Checkbox
                  style={{ marginTop: 4 }}
                  disabled={isMock}
                  checked={selectedGenIds.includes(a.id)}
                  onChange={(e) => toggleGenSelect(a.id, e.target.checked)}
                >
                  接入渲染{isMock ? '（mock 不可选）' : ''}
                </Checkbox>
              </div>
            )
          })}
          {hookArts.length === 0 && (
            <span style={{ color: '#999' }}>暂无生成素材：先预估费用并确认生成（mock 零费用可试跑）</span>
          )}
        </div>
        {opts.use_gen_hooks && (
          <Alert
            type="warning" showIcon style={{ marginTop: 8 }}
            message={selectedGenIds.length > 0
              ? `已勾选 ${selectedGenIds.length} 条素材接入渲染（L4 优先替代原生冷开场）`
              : '未勾选素材：自动取各候选最新非 mock 素材；mock 素材一律禁入真实渲染'}
          />
        )}
      </Card>

      <Card
        size="small" title="成片审阅"
        extra={(
          <Space>
            <span>安全区遮罩</span>
            <Switch size="small" checked={showMask} onChange={setShowMask} />
            <Select
              size="small" style={{ width: 110 }} value={maskPlatform} onChange={setMaskPlatform}
              options={Object.keys(presets).map((k) => ({ value: k, label: presets[k].name }))}
            />
          </Space>
        )}
      >
        <Space wrap style={{ marginBottom: 12 }}>
          {mp4s.map((a) => (
            <Button key={a.id} type={selected?.id === a.id ? 'primary' : 'default'}
              onClick={() => setSelected(a)}>
              {a.candidate_id?.slice(0, 8)}
            </Button>
          ))}
          {mp4s.length === 0 && <span style={{ color: '#999' }}>暂无成片，先采纳候选并渲染</span>}
        </Space>
        {selected && (
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            <div style={{ position: 'relative', width: 320, background: '#000' }}>
              <video
                key={selected.id} controls style={{ width: '100%', display: 'block' }}
                src={studioApi.artifactFileUrl(selected.id)}
              />
              {showMask && preset && (
                <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
                  <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: `${preset.safe_area.top * 100}%`, background: 'rgba(255,0,0,0.25)' }} />
                  <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, height: `${preset.safe_area.bottom * 100}%`, background: 'rgba(255,0,0,0.25)' }} />
                  <div style={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: `${preset.safe_area.left * 100}%`, background: 'rgba(255,0,0,0.25)' }} />
                  <div style={{ position: 'absolute', top: 0, bottom: 0, right: 0, width: `${preset.safe_area.right * 100}%`, background: 'rgba(255,0,0,0.25)' }} />
                </div>
              )}
            </div>
            <div style={{ flex: 1, minWidth: 280 }}>
              {coverOf(selected.candidate_id) && (
                <img
                  alt="封面" style={{ width: 120, marginBottom: 8 }}
                  src={studioApi.artifactFileUrl(coverOf(selected.candidate_id)!.id)}
                />
              )}
              <Descriptions size="small" column={1}>
                <Descriptions.Item label="时长">
                  {selected.meta?.selfcheck?.duration_s ?? '—'} s
                </Descriptions.Item>
                <Descriptions.Item label="画幅">
                  {selected.meta?.portrait ? `竖屏（${selected.meta?.portrait_mode}）` : '横屏'}
                </Descriptions.Item>
                <Descriptions.Item label="缺陷扫描">
                  {(selected.meta?.selfcheck?.defects || []).length === 0
                    ? <Tag color="green">无</Tag>
                    : (selected.meta?.selfcheck?.defects || []).map((d: any, i: number) => (
                      <Tag color="orange" key={i}>{d.type} {d.start_s}s</Tag>
                    ))}
                </Descriptions.Item>
              </Descriptions>
              <Collapse
                size="small" style={{ marginTop: 8 }}
                items={[{
                  key: 'meta', label: '完整回执（剪辑/钩子/响度/自检）',
                  children: <pre style={{ maxHeight: 300, overflow: 'auto', fontSize: 12 }}>{JSON.stringify(selected.meta, null, 2)}</pre>,
                }]}
              />
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}