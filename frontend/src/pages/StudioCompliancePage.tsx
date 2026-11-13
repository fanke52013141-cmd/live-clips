// 合规与发布页（前端批 F-5）：合规报告 / 留证包 / 平台发布包 / 剪映草稿导出。
import { useEffect, useState } from 'react'
import {
  Alert, Button, Card, Descriptions, Input, Select, Space, Table, Tag, message,
} from 'antd'
import {
  studioApi, fmtMs,
  type ComplianceReport, type PackReceipt, type PlatformPreset, type StudioProject,
} from '../services/studioApi'

const LICENSE_TAG: Record<string, { color: string; text: string }> = {
  ok: { color: 'green', text: '授权有效' },
  declaration_required: { color: 'orange', text: '需授权声明' },
  expired: { color: 'red', text: '授权过期' },
  scope_mismatch: { color: 'orange', text: '范围不符' },
  broken_reference: { color: 'red', text: '引用缺失' },
}

export default function StudioCompliancePage() {
  const [projects, setProjects] = useState<StudioProject[]>([])
  const [pid, setPid] = useState('')
  const [presets, setPresets] = useState<Record<string, PlatformPreset>>({})
  const [platform, setPlatform] = useState('douyin')
  const [report, setReport] = useState<ComplianceReport | null>(null)
  const [packPlatforms, setPackPlatforms] = useState<string[]>(['douyin'])
  const [pack, setPack] = useState<PackReceipt | null>(null)
  const [draftBase, setDraftBase] = useState('http://127.0.0.1:30000')
  const [draftReceipt, setDraftReceipt] = useState<Record<string, any> | null>(null)
  const [busy, setBusy] = useState('')

  useEffect(() => {
    studioApi.projects().then(setProjects).catch((e) => message.error(String(e)))
    studioApi.platforms().then(setPresets).catch(() => undefined)
  }, [])

  const loadReport = async (project: string, plat: string) => {
    if (!project) return
    setBusy('report')
    try { setReport(await studioApi.compliance(project, plat)) }
    catch (e) { message.error(String(e)) }
    finally { setBusy('') }
  }

  const onProject = (v: string) => {
    setPid(v); setReport(null); setPack(null); setDraftReceipt(null)
    loadReport(v, platform)
  }

  const doEvidence = async () => {
    setBusy('evidence')
    try {
      const r = await studioApi.evidence(pid, platform)
      message.success(`留证包已生成：${r.evidence_path}`)
      setReport(r)
    } catch (e) { message.error(String(e)) }
    finally { setBusy('') }
  }

  const doPack = async () => {
    setBusy('pack')
    try { setPack(await studioApi.pack(pid, packPlatforms)) }
    catch (e) { message.error(String(e)) }
    finally { setBusy('') }
  }

  const doDraft = async () => {
    setBusy('draft')
    try { setDraftReceipt(await studioApi.draft(pid, { base_url: draftBase })) }
    catch (e) { message.error(String(e)) }
    finally { setBusy('') }
  }

  const lic = report ? LICENSE_TAG[report.license.status] || { color: 'default', text: report.license.status } : null

  return (
    <div style={{ padding: 16, display: 'grid', gap: 16 }}>
      <Card size="small" title="合规与发布">
        <Space wrap>
          <Select
            style={{ width: 260 }} placeholder="选择项目" value={pid || undefined} onChange={onProject}
            options={projects.map((p) => ({ value: p.id, label: p.name }))}
          />
          <Select
            style={{ width: 120 }} value={platform}
            onChange={(v) => { setPlatform(v); loadReport(pid, v) }}
            options={Object.keys(presets).map((k) => ({ value: k, label: presets[k].name }))}
          />
          <Button disabled={!pid} loading={busy === 'report'} onClick={() => loadReport(pid, platform)}>
            刷新报告
          </Button>
          <Button disabled={!pid} loading={busy === 'evidence'} onClick={doEvidence}>生成留证包</Button>
        </Space>
      </Card>

      {report && (
        <Card size="small" title="合规报告">
          {report.blockers.length > 0 && (
            <Alert type="error" showIcon style={{ marginBottom: 12 }}
              message="阻断项" description={report.blockers.join('；')} />
          )}
          <Descriptions size="small" column={2} style={{ marginBottom: 12 }}>
            <Descriptions.Item label="授权">
              <Tag color={lic!.color}>{lic!.text}</Tag>
              {report.license.note || report.license.licensor || ''}
            </Descriptions.Item>
            <Descriptions.Item label="生成时间">{report.generated_at}</Descriptions.Item>
          </Descriptions>
          <Table
            size="small" rowKey={(r) => `${r.field}-${r.word}-${r.pos}-${r.start_ms ?? ''}`}
            dataSource={report.banned_words.hits}
            pagination={{ pageSize: 8 }}
            locale={{ emptyText: '违禁词零命中' }}
            columns={[
              { title: '位置', dataIndex: 'field', width: 90 },
              { title: '命中词', dataIndex: 'word', width: 120, render: (w) => <Tag color="red">{w}</Tag> },
              { title: '上下文', dataIndex: 'context', ellipsis: true },
              {
                title: '时间码', width: 110,
                render: (_, r) => (r.start_ms != null ? `${fmtMs(r.start_ms)}–${fmtMs(r.end_ms)}` : '—'),
              },
            ]}
          />
          <Table
            size="small" rowKey="artifact_id" style={{ marginTop: 12 }}
            dataSource={report.artifacts} pagination={false}
            columns={[
              { title: '产物', dataIndex: 'artifact_id', width: 120, render: (v) => v.slice(0, 8) },
              {
                title: '变形度自评', width: 160,
                render: (_, r) => (
                  <Space>
                    <Tag color={r.transform.score >= 40 ? 'green' : 'orange'}>{r.transform.score}</Tag>
                    <span style={{ fontSize: 12, color: '#999' }}>启发式</span>
                  </Space>
                ),
              },
              {
                title: '加工要素', render: (_, r) => r.transform.factors.map((f) => (
                  <Tag key={f.factor}>{f.factor}+{f.weight}</Tag>
                )),
              },
              {
                title: 'AIGC 标识', width: 220,
                render: (_, r) => r.aigc.required ? (
                  <Space size={4} wrap>
                    <Tag color={r.aigc.labeled ? 'green' : 'red'}>
                      {r.aigc.labeled ? '元数据已标识' : '元数据缺失'}
                    </Tag>
                    <Tag color={r.aigc.badge_burned ? 'green' : 'red'}>
                      {r.aigc.badge_burned ? '角标已烧录' : '角标缺失'}
                    </Tag>
                    {!r.aigc.ok && <Tag color="red">不合规</Tag>}
                  </Space>
                ) : <Tag>无需标识</Tag>,
              },
            ]}
          />
          {report.evidence_path && (
            <Alert type="info" style={{ marginTop: 12 }} message={`留证包：${report.evidence_path}`} />
          )}
        </Card>
      )}

      <Card size="small" title="平台发布包">
        <Space wrap>
          <Select
            mode="multiple" style={{ minWidth: 280 }} value={packPlatforms} onChange={setPackPlatforms}
            options={Object.keys(presets).map((k) => ({ value: k, label: presets[k].name }))}
          />
          <Button type="primary" disabled={!pid || packPlatforms.length === 0}
            loading={busy === 'pack'} onClick={doPack}>
            构建发布包
          </Button>
          {pack && (
            <Button href={studioApi.artifactFileUrl(pack.artifact_id)} target="_blank">
              下载 ZIP（{(pack.zip_bytes / 1024 / 1024).toFixed(1)} MB）
            </Button>
          )}
        </Space>
        {pack?.warnings.map((w, i) => (
          <Alert key={i} type="warning" showIcon style={{ marginTop: 8 }} message={w} />
        ))}
        {pack && pack.warnings.length === 0 && (
          <Alert type="success" style={{ marginTop: 8 }} message="无警告：画幅匹配、文案未超限" />
        )}
      </Card>

      <Card size="small" title="剪映草稿导出（capcut-mate，Apache-2.0）">
        <Space wrap>
          <Input
            style={{ width: 280 }} value={draftBase} onChange={(e) => setDraftBase(e.target.value)}
            placeholder="capcut-mate 服务地址"
          />
          <Button disabled={!pid} loading={busy === 'draft'} onClick={doDraft}>导出草稿</Button>
        </Space>
        {draftReceipt && (
          <div style={{ marginTop: 12 }}>
            {(draftReceipt.drafts || []).map((d: any) => (
              <Alert
                key={d.candidate_id} type="info" style={{ marginBottom: 8 }}
                message={`草稿 ${d.draft_id || ''}（字幕 ${d.captions} 条）`}
                description={`${d.draft_url || ''}${d.url_note ? `；${d.url_note}` : ''}；${d.format_version_note}`}
              />
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}