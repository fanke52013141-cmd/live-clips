// SliceStudio 逐字稿页（P1-6c）：句级时间码 + 词级时间戳 + 热词 + 引擎信息。
// P1 为只读审阅；即点改错/热词管理在 P2 与转写纠错页合并实现。
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Alert, Button, Card, Empty, Space, Spin, Tag, message } from 'antd'
import { StudioTranscript, fmtMs, studioApi } from '../services/studioApi'

export default function StudioTranscriptPage() {
  const { id } = useParams<{ id: string }>()
  const [tr, setTr] = useState<StudioTranscript | null>(null)
  const [loading, setLoading] = useState(true)
  const [showWords, setShowWords] = useState(false)

  useEffect(() => {
    if (!id) return
    setLoading(true)
    studioApi.transcript(id)
      .then(setTr)
      .catch((e) => message.error(`加载失败: ${(e as Error).message}`))
      .finally(() => setLoading(false))
  }, [id])

  if (loading) return <Spin style={{ margin: 48 }} />

  return (
    <div style={{ padding: 16, maxWidth: 900, margin: '0 auto' }}>
      <Space style={{ marginBottom: 12 }}>
        <Link to={`/studio/review/${id}`}><Button>返回审核台</Button></Link>
        {tr && (
          <Button onClick={() => setShowWords((v) => !v)}>
            {showWords ? '隐藏词级时间戳' : '显示词级时间戳'}
          </Button>
        )}
      </Space>

      {!tr ? (
        <Empty description="暂无转写（先在审核台运行管线）" />
      ) : (
        <>
          <Alert
            style={{ marginBottom: 12 }}
            type="info"
            message={`引擎: ${tr.engine} / ${tr.model}｜词级: ${tr.word_level ? '是' : '否（句级）'}`}
            description={tr.hotwords?.length ? `热词: ${tr.hotwords.join('、')}` : undefined}
          />
          <Card size="small">
            {tr.sentences.map((s, i) => (
              <div key={i} style={{ padding: '6px 0', borderBottom: '1px solid #f0f0f0' }}>
                <Tag color="blue" style={{ fontFamily: 'monospace' }}>
                  {fmtMs(s.start_ms)}-{fmtMs(s.end_ms)}
                </Tag>
                <span>{s.text}</span>
              </div>
            ))}
          </Card>
          {showWords && (
            <Card size="small" title={`词级时间戳（${tr.words.length}）`} style={{ marginTop: 12 }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                {tr.words.map((w, i) => (
                  <Tag key={i} style={{ fontFamily: 'monospace' }}>
                    {w.text ?? w.w} {w.start_ms}-{w.end_ms}
                  </Tag>
                ))}
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  )
}