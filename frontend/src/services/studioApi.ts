// SliceStudio API 封装（P1-6c）。
// 基址默认本机 studio 服务；可在设置页/localStorage 覆盖（studioApiBase）。
// 注意：不使用 import.meta（上游 vm 沙箱测试在 Node24 下的已知基线问题）。

export interface StudioCandidate {
  id: string
  project_id: string
  layer: 'clips0' | 'clips1'
  parent_id: string | null
  start_ms: number
  end_ms: number
  clip_type: string | null
  scores: Record<string, unknown> | null
  rank_score: number | null
  quote: string | null
  reason: string | null
  title: string | null
  caption: string | null
  hook_plan: Record<string, unknown> | null
  adopted: boolean | null
  rejected_reason: string | null
  created_by: string
}

export interface StudioTask {
  id: string
  kind: string
  stage: string
  status: string
  progress: {
    stage?: string
    done?: number
    total?: number
    unit?: string
    message?: string
    degraded?: { engine: string; reason: string }[]
  } | null
  receipt: Record<string, unknown> | null
  error: string | null
}

export interface StudioProject {
  id: string
  name: string
  live_type: string
  status: string
  material_id: string
}

export interface StudioMaterial {
  id: string
  file_path: string | null
  duration_ms: number | null
  danmaku_path: string | null
}

export interface StudioTranscript {
  engine: string
  model: string
  word_level: boolean
  words: { text?: string; w?: string; start_ms: number; end_ms: number }[]
  sentences: { text: string; start_ms: number; end_ms: number }[]
  hotwords: string[] | null
}

export interface StudioArtifact {
  id: string
  candidate_id: string | null
  kind: string
  platform: string | null
  file_path: string
  size_bytes: number | null
  meta: Record<string, any> | null
}

export interface RenderOptions {
  platform?: string
  portrait?: boolean
  subtitles?: boolean
  debreath?: boolean
  filler_cut?: boolean
  keep_breath_ms?: number
  cold_open?: boolean
  cold_open_jumpcut?: boolean
  flash_forward?: boolean
  l1_overlay?: boolean
  l2_tts?: boolean
  l2_rank_threshold?: number
  use_gen_hooks?: boolean
  gen_asset_ids?: string[]
}

export interface HookEstimate {
  level: number
  provider: string
  items: {
    candidate_id: string
    title: string | null
    rank_score: number | null
    prompt: string
    estimate: { provider: string; kind: string; unit_price: number; total: number; currency: string; note: string }
  }[]
  total: number
  currency: string
  gate: string
  confirm_required: boolean
  note: string
}

export interface HookGenResult {
  level: number
  provider: string
  confirmed: boolean
  estimated_total: number
  generated: { candidate_id: string; artifact_id: string; file_path: string; mock: boolean }[]
  skipped: { candidate_id: string; reason: string }[]
  aigc_note: string
}

export interface PlatformPreset {
  name: string
  aspect: string
  width: number
  height: number
  cover_ratio: string
  title_max: number
  caption_max: number
  safe_area: { top: number; bottom: number; left: number; right: number }
}

export interface ComplianceReport {
  project_id: string
  generated_at: string
  banned_words: { hits: Record<string, any>[]; wordlist_version: string }
  license: { status: string; note?: string; licensor?: string }
  artifacts: {
    artifact_id: string
    candidate_id: string | null
    transform: { score: number; factors: { factor: string; weight: number }[]; disclaimer: string }
    aigc: { required: boolean; labeled: boolean; badge_burned: boolean; ok: boolean; note: string }
  }[]
  lineage: Record<string, any>[]
  blockers: string[]
  evidence_path?: string
}

export interface PackReceipt {
  platforms: string[]
  clips: number
  zip_path: string
  zip_bytes: number
  warnings: string[]
  artifact_id: string
}

export function studioBase(): string {
  return localStorage.getItem('studioApiBase') || 'http://127.0.0.1:26761'
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(studioBase() + path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = `${res.status}`
    try {
      const body = await res.json()
      if (body && body.detail) detail = String(body.detail)
    } catch {
      // 非 JSON 错误体，保留状态码
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

export const studioApi = {
  materials: () => req<StudioMaterial[]>('/studio/materials'),
  ingestMaterial: (file_path: string, danmaku_path?: string) =>
    req<StudioMaterial>('/studio/materials', {
      method: 'POST',
      body: JSON.stringify({ file_path, danmaku_path: danmaku_path || null }),
    }),
  projects: () => req<StudioProject[]>('/studio/projects'),
  createProject: (body: {
    material_id: string
    name: string
    live_type: string
    intent?: string
    background?: string
  }) => req<{ id: string }>('/studio/projects', { method: 'POST', body: JSON.stringify(body) }),
  candidates: (pid: string, layer?: string) =>
    req<StudioCandidate[]>(
      `/studio/projects/${pid}/candidates${layer ? `?layer=${layer}` : ''}`,
    ),
  adopt: (cid: string, body: { user: string; start_ms?: number; end_ms?: number; title?: string }) =>
    req<{ clips1_id: string }>(`/studio/candidates/${cid}/adopt`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  reject: (cid: string, user: string, reason: string) =>
    req<{ clips1_id: string }>(`/studio/candidates/${cid}/reject`, {
      method: 'POST',
      body: JSON.stringify({ user, reason }),
    }),
  patchClips1: (
    cid: string,
    body: {
      user: string
      start_ms?: number
      end_ms?: number
      title?: string
      caption?: string
      hook_plan?: Record<string, unknown>
      adopted?: boolean
    },
  ) => req<StudioCandidate>(`/studio/clips1/${cid}`, { method: 'PATCH', body: JSON.stringify(body) }),
  transcript: (pid: string) => req<StudioTranscript | null>(`/studio/projects/${pid}/transcript`),
  signals: (pid: string) =>
    req<Record<string, { bucket_ms: number; series: Record<string, number[]> }> | null>(
      `/studio/projects/${pid}/signals`,
    ),
  waveform: (pid: string) =>
    req<{ peaks: number[]; buckets: number; duration_ms: number; cached: boolean }>(
      `/studio/projects/${pid}/waveform`,
    ),
  materialFileUrl: (mid: string) => `${studioBase()}/studio/materials/${mid}/file`,
  task: (pid: string) => req<StudioTask | null>(`/studio/projects/${pid}/task`),
  run: (pid: string) => req<{ started: boolean }>(`/studio/projects/${pid}/run`, { method: 'POST' }),
  cancel: (pid: string) =>
    req<{ cancelled: boolean }>(`/studio/projects/${pid}/cancel`, { method: 'POST' }),
  // ---- 渲染/产物/合规/发布（前端批 F-2） ----
  render: (pid: string, opts: RenderOptions) =>
    req<{ started: boolean }>(`/studio/projects/${pid}/render`, {
      method: 'POST',
      body: JSON.stringify(opts),
    }),
  artifacts: (pid: string) => req<StudioArtifact[]>(`/studio/projects/${pid}/artifacts`),
  artifactFileUrl: (aid: string) => `${studioBase()}/studio/artifacts/${aid}/file`,
  compliance: (pid: string, platform?: string) =>
    req<ComplianceReport>(
      `/studio/projects/${pid}/compliance${platform ? `?platform=${platform}` : ''}`,
    ),
  evidence: (pid: string, platform?: string) =>
    req<ComplianceReport>(`/studio/projects/${pid}/evidence`, {
      method: 'POST',
      body: JSON.stringify({ platform: platform || null }),
    }),
  pack: (pid: string, platforms: string[]) =>
    req<PackReceipt>(`/studio/projects/${pid}/pack`, {
      method: 'POST',
      body: JSON.stringify({ platforms }),
    }),
  platforms: () => req<Record<string, PlatformPreset>>('/studio/platforms'),
  draft: (pid: string, body: { base_url: string; video_urls?: Record<string, string>; portrait?: boolean }) =>
    req<Record<string, any>>(`/studio/projects/${pid}/draft`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  hookEstimate: (pid: string, body: { level: number; provider?: string; candidate_ids?: string[] }) =>
    req<HookEstimate>(`/studio/projects/${pid}/hooks/estimate`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  hookGenerate: (
    pid: string,
    body: { level: number; provider?: string; candidate_ids?: string[]; confirm: boolean; max_cost?: number },
  ) =>
    req<HookGenResult>(`/studio/projects/${pid}/hooks/generate`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}

export function fmtMs(ms: number): string {
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}