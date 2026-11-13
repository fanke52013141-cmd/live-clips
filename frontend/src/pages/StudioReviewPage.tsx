// SliceStudio 候选审核台（P1-6c 核心页）。
// 理念：AI 出候选（clips0 只读），人工采纳/否决/精修落 clips1，全程可溯源。
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Alert, Button, Card, Form, Input, InputNumber, Modal, Select, Space, Table, Tag, message,
} from 'antd'
import {
  StudioCandidate, StudioProject, StudioTask, fmtMs, studioApi,
} from '../services/studioApi'
import StudioTimelinePanel from '../components/StudioTimelinePanel'

const USER = 'reviewer' // P1 单用户；多用户鉴权随上游 JWT 缝合

type Tracks = Record<string, { bucket_ms: number; series: Record<string, number[]> }>

export default function StudioReviewPage() {
  const { id } = useParams<{ id: string }>()
  const [projects, setProjects] = useState<StudioProject[]>([])
  const [pid, setPid] = useState<string | undefined>(id)
  const [clips0, setClips0] = useState<StudioCandidate[]>([])
  const [clips1, setClips1] = useState<StudioCandidate[]>([])
  const [task, setTask] = useState<StudioTask | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [adoptTarget, setAdoptTarget] = useState<StudioCandidate | null>(null)
  const [rejectTarget, setRejectTarget] = useState<StudioCandidate | null>(null)
  const [editTarget, setEditTarget] = useState<StudioCandidate | null>(null)
  const [words, setWords] = useState<{ text: string; start_ms: number; end_ms: number }[]>([])
  const [signals, setSignals] = useState<Tracks | null>(null)
  const [waveform, setWaveform] = useState<number[] | null>(null)
  const [materialId, setMaterialId] = useState<string | null>(null)
  const [durationMs, setDurationMs] = useState(0)
  const [createForm] = Form.useForm()
  const [adoptForm] = Form.useForm()
  const [rejectForm] = Form.useForm()
  const [editForm] = Form.useForm()
  const timer = useRef<number | null>(null)

  const refresh = useCallback(async (target?: string) => {
    const p = target ?? pid
    try {
      const ps = await studioApi.projects()
      setProjects(ps)
      if (!p) return
      const [c0, c1, t, tr, sig, mats, wf] = await Promise.all([
        studioApi.candidates(p, 'clips0'),
        studioApi.candidates(p, 'clips1'),
        studioApi.task(p),
        studioApi.transcript(p),
        studioApi.signals(p),
        studioApi.materials(),
        studioApi.waveform(p).catch(() => null),
      ])
      setClips0(c0)
      setClips1(c1)
      setTask(t)
      setWords(((tr?.words || []) as { text?: string; w?: string; start_ms: number; end_ms: number }[])
        .map((w) => ({ text: w.text ?? w.w ?? '', start_ms: w.start_ms, end_ms: w.end_ms })))
      setSignals(sig)
      setWaveform(wf?.peaks ?? null)
      const proj = ps.find((x) => x.id === p)
      const mat = mats.find((m) => m.id === proj?.material_id)
      setMaterialId(mat?.id ?? null)
      setDurationMs(mat?.duration_ms ?? 0)
    } catch (e) {
      message.error(`加载失败: ${(e as Error).message}（请确认 studio API 已启动）`)
    }
  }, [pid])

  useEffect(() => { void refresh() }, [refresh])

  // 运行中轮询进度（数字增长可见）
  useEffect(() => {
    const running = task && !['done', 'failed', 'cancelled'].includes(task.status)
    if (running && pid) {
      timer.current = window.setTimeout(() => void refresh(), 1500)
    }
    return () => { if (timer.current) window.clearTimeout(timer.current) }
  }, [task, pid, refresh])

  const running = !!task && !['done', 'failed', 'cancelled'].includes(task.status)
  const prog = task?.progress

  const doCreate = async () => {
    const v = await createForm.validateFields()
    try {
      const m = await studioApi.ingestMaterial(v.file_path, v.danmaku_path)
      const p = await studioApi.createProject({
        material_id: m.id, name: v.name, live_type: v.live_type,
        intent: v.intent, background: v.background,
      })
      message.success('项目已创建')
      setCreateOpen(false)
      createForm.resetFields()
      setPid(p.id)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const doAdopt = async () => {
    if (!adoptTarget) return
    const v = await adoptForm.validateFields()
    try {
      await studioApi.adopt(adoptTarget.id, {
        user: USER, start_ms: v.start_ms, end_ms: v.end_ms, title: v.title,
      })
      message.success('已采纳为 clips1')
      setAdoptTarget(null)
      void refresh()
    } catch (e) { message.error((e as Error).message) }
  }

  const doReject = async () => {
    if (!rejectTarget) return
    const v = await rejectForm.validateFields()
    try {
      await studioApi.reject(rejectTarget.id, USER, v.reason)
      message.success('已否决（留痕）')
      setRejectTarget(null)
      void refresh()
    } catch (e) { message.error((e as Error).message) }
  }

  const doEdit = async () => {
    if (!editTarget) return
    const v = await editForm.validateFields()
    try {
      await studioApi.patchClips1(editTarget.id, { user: USER, ...v })
      message.success('已精修')
      setEditTarget(null)
      void refresh()
    } catch (e) { message.error((e as Error).message) }
  }

  const weak = (c: StudioCandidate) =>
    !!(c.scores && (c.scores as Record<string, unknown>).not_recommended)

  const cols0 = [
    { title: '分', dataIndex: 'rank_score', width: 110,
      render: (v: number | null, c: StudioCandidate) => (
        <Space size={4}>
          <span style={{ color: weak(c) ? '#999' : undefined }}>{v ?? '-'}</span>
          {weak(c) && <Tag color="orange" style={{ fontSize: 10 }}>不建议</Tag>}
        </Space>
      ) },
    { title: '类型', dataIndex: 'clip_type', width: 70 },
    { title: '区间', width: 110,
      render: (_: unknown, c: StudioCandidate) => `${fmtMs(c.start_ms)}-${fmtMs(c.end_ms)}` },
    { title: '标题', dataIndex: 'title', ellipsis: true },
    { title: '理由', dataIndex: 'reason', ellipsis: true },
    { title: '操作', width: 150, render: (_: unknown, c: StudioCandidate) => (
      <Space>
        <Button size="small" type="primary" onClick={() => {
          adoptForm.setFieldsValue({ start_ms: c.start_ms, end_ms: c.end_ms, title: c.title })
          setAdoptTarget(c)
        }}>采纳</Button>
        <Button size="small" danger onClick={() => {
          rejectForm.resetFields()
          setRejectTarget(c)
        }}>否决</Button>
      </Space>
    ) },
  ]

  const cols1 = [
    { title: '状态', width: 90, render: (_: unknown, c: StudioCandidate) =>
      c.adopted ? <Tag color="green">已采纳</Tag> : <Tag color="red">已否决</Tag> },
    { title: '区间', width: 110,
      render: (_: unknown, c: StudioCandidate) => `${fmtMs(c.start_ms)}-${fmtMs(c.end_ms)}` },
    { title: '标题', dataIndex: 'title', ellipsis: true },
    { title: '否决理由', dataIndex: 'rejected_reason', ellipsis: true },
    { title: '操作者', dataIndex: 'created_by', width: 110 },
    { title: '操作', width: 80, render: (_: unknown, c: StudioCandidate) => (
      <Button size="small" onClick={() => {
        editForm.setFieldsValue({
          start_ms: c.start_ms, end_ms: c.end_ms, title: c.title,
          caption: c.caption, adopted: c.adopted ?? false,
        })
        setEditTarget(c)
      }}>精修</Button>
    ) },
  ]

  return (
    <div style={{ padding: 16, maxWidth: 1200, margin: '0 auto' }}>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          style={{ width: 280 }}
          placeholder="选择项目"
          value={pid}
          onChange={(v) => setPid(v)}
          options={projects.map((p) => ({ value: p.id, label: `${p.name}（${p.status}）` }))}
        />
        <Button onClick={() => void refresh()}>刷新</Button>
        <Button onClick={() => { createForm.resetFields(); setCreateOpen(true) }}>新建项目</Button>
        {pid && (
          <>
            <Button type="primary" disabled={running} onClick={async () => {
              try { await studioApi.run(pid); message.success('管线已启动'); void refresh() }
              catch (e) { message.error((e as Error).message) }
            }}>运行管线</Button>
            <Button danger disabled={!running} onClick={async () => {
              await studioApi.cancel(pid); void refresh()
            }}>取消</Button>
            <Link to={`/studio/transcript/${pid}`}><Button>逐字稿</Button></Link>
            <Link to="/studio/render"><Button>渲染审阅</Button></Link>
            <Link to="/studio/compliance"><Button>合规发布</Button></Link>
          </>
        )}
      </Space>

      {task && (
        <Alert
          style={{ marginBottom: 12 }}
          type={task.status === 'failed' ? 'error' : running ? 'info' : 'success'}
          message={
            prog
              ? `${prog.stage ?? task.stage}: ${prog.done ?? 0}/${prog.total ?? 0} ${prog.unit ?? ''} ${prog.message ?? ''}`
              : `任务 ${task.status}`
          }
          description={
            <>
              {task.error && <div>错误: {task.error}</div>}
              {prog?.degraded?.map((d, i) => (
                <div key={i} style={{ color: '#999' }}>降级: {d.engine} — {d.reason}</div>
              ))}
            </>
          }
        />
      )}

      {pid && (
        <StudioTimelinePanel
          materialId={materialId}
          durationMs={durationMs}
          words={words}
          signals={signals}
          waveform={waveform}
          clips0={clips0}
          clips1={clips1}
          onChanged={() => void refresh()}
        />
      )}

      <Card title={`AI 候选 clips0（${clips0.length}）`} size="small" style={{ marginBottom: 12 }}>
        <Table
          rowKey="id" size="small" columns={cols0} dataSource={clips0}
          pagination={false}
          rowClassName={(c) => (weak(c) ? 'studio-weak-row' : '')}
        />
      </Card>

      <Card title={`人工决定 clips1（${clips1.length}）`} size="small">
        <Table rowKey="id" size="small" columns={cols1} dataSource={clips1} pagination={false} />
      </Card>

      <Modal title="新建项目（本地素材入库）" open={createOpen} onOk={doCreate}
        onCancel={() => setCreateOpen(false)} destroyOnClose>
        <Form form={createForm} layout="vertical">
          <Form.Item name="file_path" label="视频文件绝对路径" rules={[{ required: true }]}>
            <Input placeholder="D:\videos\live.mp4" />
          </Form.Item>
          <Form.Item name="danmaku_path" label="弹幕文件（可选，录播姬 xml / 抖音 jsonl）">
            <Input />
          </Form.Item>
          <Form.Item name="name" label="项目名" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="live_type" label="直播类型" initialValue="ecommerce" rules={[{ required: true }]}>
            <Select options={[
              { value: 'ecommerce', label: '带货' },
              { value: 'entertainment', label: '娱乐' },
              { value: 'knowledge', label: '知识' },
              { value: 'game', label: '游戏' },
              { value: 'talent', label: '才艺' },
            ]} />
          </Form.Item>
          <Form.Item name="intent" label="定向（自然语言，可选）"><Input /></Form.Item>
          <Form.Item name="background" label="背景（主播/术语，可选）"><Input /></Form.Item>
        </Form>
      </Modal>

      <Modal title="采纳为 clips1（可微调）" open={!!adoptTarget} onOk={doAdopt}
        onCancel={() => setAdoptTarget(null)} destroyOnClose>
        <Form form={adoptForm} layout="vertical">
          <Form.Item name="start_ms" label="入点 ms" rules={[{ required: true }]}>
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="end_ms" label="出点 ms" rules={[{ required: true }]}>
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="title" label="标题"><Input /></Form.Item>
        </Form>
      </Modal>

      <Modal title="否决（必须给理由，留痕）" open={!!rejectTarget} onOk={doReject}
        onCancel={() => setRejectTarget(null)} destroyOnClose>
        <Form form={rejectForm} layout="vertical">
          <Form.Item name="reason" label="理由" rules={[{ required: true, whitespace: true }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title="精修 clips1" open={!!editTarget} onOk={doEdit}
        onCancel={() => setEditTarget(null)} destroyOnClose>
        <Form form={editForm} layout="vertical">
          <Form.Item name="start_ms" label="入点 ms" rules={[{ required: true }]}>
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="end_ms" label="出点 ms" rules={[{ required: true }]}>
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="title" label="标题"><Input /></Form.Item>
          <Form.Item name="caption" label="文案"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item name="adopted" label="采纳状态">
            <Select options={[{ value: true, label: '已采纳' }, { value: false, label: '未采纳' }]} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}