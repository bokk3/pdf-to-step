import { useEffect, useRef, useState } from 'react'
import type { DrawingInfo, DrawingLanguage, JobDetail, JobSummary, PartSpec, PipelineEvent, SheetSize } from '../types/partSpec'

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: init?.body instanceof FormData ? init.headers : { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export interface Health {
  status: string
  bind: string
  deepseek: {
    configured: boolean
    mock: boolean
    vision_model: string
    code_model: string
    thinking: string
    files_api: boolean
    reachable?: boolean
    error?: string
  }
  occt?: { ok: boolean; build123d?: string; ocp?: string; error?: string }
  critic_enabled: boolean
  max_heal_retries: number
  step_schema: string
}

/** Pydantic 422 detail -> readable lines. */
export function validationLines(e: unknown): string[] {
  if (e instanceof ApiError && Array.isArray(e.detail)) {
    return (e.detail as { loc: (string | number)[]; msg: string }[]).map((d) => `${d.loc.join('.')}: ${d.msg}`)
  }
  return [e instanceof Error ? e.message : String(e)]
}

export type UploadKind = 'pdf' | 'dxf' | 'step' | 'sketch'
const UPLOAD_ENDPOINT: Record<UploadKind, string> = {
  pdf: '/api/jobs',
  dxf: '/api/jobs/from-dxf',
  step: '/api/jobs/from-step',
  sketch: '/api/jobs/from-sketch',
}

export function uploadKind(file: File): UploadKind | null {
  const n = file.name.toLowerCase()
  if (n.endsWith('.pdf') || file.type === 'application/pdf') return 'pdf'
  if (n.endsWith('.dxf')) return 'dxf'
  if (n.endsWith('.step') || n.endsWith('.stp')) return 'step'
  if (/^image\/(jpeg|png|webp)$/.test(file.type) || /\.(jpe?g|png|webp)$/.test(n)) return 'sketch'
  return null
}

export const api = {
  health: (deep = false) => request<Health>(`/api/health${deep ? '?deep=true' : ''}`),
  listJobs: () => request<JobSummary[]>('/api/jobs'),
  getJob: (id: string, version?: number | null) =>
    request<JobDetail>(`/api/jobs/${id}${version ? `?version=${version}` : ''}`),
  deleteJob: (id: string) => request<void>(`/api/jobs/${id}`, { method: 'DELETE' }),
  /** PDF -> vision pipeline; DXF / STEP -> deterministic intake; photo -> sketch template (DRAWING_SPEC §6). */
  upload: (file: File, kind: UploadKind = 'pdf') => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ job_id: string; status: string; filename: string }>(UPLOAD_ENDPOINT[kind], { method: 'POST', body: fd })
  },
  /** §7 PATCH /spec: deterministic rebuild, no AI call; returns the new version's state. */
  patchSpec: (id: string, spec: PartSpec) =>
    request<JobDetail>(`/api/jobs/${id}/spec`, { method: 'PATCH', body: JSON.stringify(spec) }),
  /** DRAWING_SPEC §6: (re)generate the EN ISO drawing of the current version, optional overrides. */
  generateDrawing: (
    id: string,
    overrides: { sheet_size?: SheetSize; scale?: string; language?: DrawingLanguage; section_view?: 'AUTO' | 'NONE'; version?: number } = {},
  ) =>
    request<DrawingInfo & { artifacts: Record<string, string> }>(`/api/jobs/${id}/drawing`, {
      method: 'POST',
      body: JSON.stringify(overrides),
    }),
  /** v1.5 "Mark verified": the operator confirms the current PartSpec (logged against the AI reading). */
  verify: (id: string) => request<{ verified: boolean; version: number }>(`/api/jobs/${id}/verify`, { method: 'POST' }),
  setupSheet: (id: string) =>
    request<{ artifacts: Record<string, string> }>(`/api/jobs/${id}/setup-sheet`, { method: 'POST' }),
  shopfloor: (id: string) => request<Shopfloor>(`/api/jobs/${id}/shopfloor`),
  listBatches: () => request<BatchStatus[]>('/api/batches'),
  getBatch: (id: string) => request<BatchStatus>(`/api/batches/${id}`),
  uploadBatch: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ batch_id: string; job_count: number; page_count: number }>('/api/batches', { method: 'POST', body: fd })
  },
  getTools: () => request<ToolLibrary>('/api/config/tools'),
  putTools: (lib: ToolLibrary) => request<ToolLibrary>('/api/config/tools', { method: 'PUT', body: JSON.stringify(lib) }),
  importTools: (file: File, replace: boolean) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ source: string; added: number; updated: number; skipped: string[]; total: number }>(`/api/config/tools/import-fusion?replace=${replace}`, { method: 'POST', body: fd })
  },
  // ---- v2.0 -----------------------------------------------------------------------------------
  setupPlan: (id: string) => request<SetupPlan & { version: number }>(`/api/jobs/${id}/setup-plan`),
  softjaws: (id: string, req: Partial<SoftjawRequest>) =>
    request<SoftjawReport>(`/api/jobs/${id}/softjaws`, { method: 'POST', body: JSON.stringify(req) }),
  getVises: () => request<{ schema_version: string; vises: ViseSpec[] }>('/api/config/vises'),
  putVises: (lib: { schema_version: string; vises: ViseSpec[] }) =>
    request<{ schema_version: string; vises: ViseSpec[] }>('/api/config/vises', { method: 'PUT', body: JSON.stringify(lib) }),
  partRevisions: (pn: string) =>
    request<{ part_number: string; revisions: PartRevision[]; by_revision: string[] }>(`/api/parts/${encodeURIComponent(pn)}/revisions`),
  revisionDiff: (job_old_id: string, job_new_id: string) =>
    request<RevisionDiffReport>('/api/revisions/diff', { method: 'POST', body: JSON.stringify({ job_old_id, job_new_id }) }),
  quote: (id: string, req: Partial<QuoteRequest>) =>
    request<QuoteBreakdown>(`/api/jobs/${id}/quote`, { method: 'POST', body: JSON.stringify(req) }),
  getRates: () => request<ShopRates>('/api/config/shop-rates'),
  putRates: (r: ShopRates) => request<ShopRates>('/api/config/shop-rates', { method: 'PUT', body: JSON.stringify(r) }),
  reextract: (id: string, hint: string) =>
    request<{ job_id: string; status: string }>(`/api/jobs/${id}/reextract`, {
      method: 'POST',
      body: JSON.stringify({ hint: hint || null }),
    }),
}

/**
 * Subscribe to a job's SSE stream (`event: stage`). Replays history, then streams live;
 * EventSource reconnects with Last-Event-ID. Bump `epoch` to resubscribe after a job restarts.
 */
export function useJobEvents(jobId: string | null, epoch: number, onEvent?: (e: PipelineEvent) => void, onEnd?: () => void) {
  const [events, setEvents] = useState<PipelineEvent[]>([])
  const [live, setLive] = useState(false)
  const cb = useRef({ onEvent, onEnd })
  cb.current = { onEvent, onEnd }

  useEffect(() => {
    setEvents([])
    if (!jobId) return
    const es = new EventSource(`/api/jobs/${jobId}/events`)
    const seen = new Set<number>()
    es.onopen = () => setLive(true)
    es.addEventListener('stage', (msg) => {
      const ev = JSON.parse((msg as MessageEvent).data) as PipelineEvent
      if (seen.has(ev.id)) return
      seen.add(ev.id)
      setEvents((prev) => [...prev, ev])
      cb.current.onEvent?.(ev)
    })
    es.addEventListener('end', () => {
      setLive(false)
      es.close()
      cb.current.onEnd?.()
    })
    es.onerror = () => setLive(false)
    return () => {
      es.close()
      setLive(false)
    }
  }, [jobId, epoch])

  return { events, live }
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

// ---- v1.5 shopfloor / batches / tool library ------------------------------------------------------
export interface CamColorInfo { name: string; hex: string; meaning: string }
export interface ShopfloorFeature {
  id: string
  type: string
  face: string | null
  label: string
  category: string
  tolerance: string | null
  ra_um: number | null
  anchors: [number, number, number][]
  tools: { operation: string; tool_id: string; tool_name: string; diameter_mm: number }[]
  issues: { status: string; operation: string; message: string; suggested_fix: string | null }[]
}
export interface Shopfloor {
  job_id: string
  version: number
  filename: string
  status: string
  verified_at: number | null
  part_name: string
  material: string
  general_tolerance: string
  datum_origin: string
  bbox: { min: [number, number, number]; max: [number, number, number] }
  stock_recommendation: { profile_label: string; recommended_blank_mm: { x: number; y: number; z: number }; raw_mass_kg: number | null } | null
  palette: Record<string, CamColorInfo>
  color_counts: Record<string, number>
  features: ShopfloorFeature[]
  artifacts: Record<string, string>
}
export interface BatchJob {
  job_id: string
  part_number: string | null
  filename: string
  pages: number[]
  status: string
  progress: number
  message: string | null
  running: boolean
  current_version: number | null
  dfm: { passed: boolean; errors: number; warnings: number; needs_review: boolean } | null
}
export interface BatchStatus {
  batch_id: string
  filename: string
  status: string
  message: string | null
  page_count: number
  created_at: number
  job_count: number
  ready_count: number
  finished_count: number
  progress: number
  jobs: BatchJob[]
}
export interface ToolSpec {
  id: string
  name: string
  tool_type: string
  diameter_mm: number
  flute_length_mm: number
  overall_length_mm?: number | null
  corner_radius_mm: number
  flutes: number
  materials: string[]
  thread_spec?: string | null
  tolerance_class?: string | null
  in_magazine: boolean
  notes?: string
}
export interface ToolLibrary { schema_version: string; tools: ToolSpec[] }

/** Batch SSE (`event: batch` snapshots until `end`). */
export function useBatchEvents(batchId: string | null, onSnap: (s: BatchStatus) => void) {
  const cb = useRef(onSnap)
  cb.current = onSnap
  useEffect(() => {
    if (!batchId) return
    const es = new EventSource(`/api/batches/${batchId}/events`)
    es.addEventListener('batch', (m) => cb.current(JSON.parse((m as MessageEvent).data) as BatchStatus))
    es.addEventListener('end', () => es.close())
    return () => es.close()
  }, [batchId])
}
// ---- v2.0 setup plan / softjaws / revisions / quotes ---------------------------------------------
export interface SetupStep {
  index: number
  normals: [number, number, number][]
  faces: string[]
  feature_ids: string[]
  deck_off: boolean
  clamping: string
  workholding: 'vise_hardjaws' | 'vise_softjaws' | 'vise_5axis'
}
export interface SetupPlan {
  min_setups_3axis: number
  min_setups_5axis_3plus2: number
  deck_off_required: boolean
  softjaws_recommended: boolean
  setups_3axis: SetupStep[]
  setups_5axis_3plus2: SetupStep[]
  notes: string[]
}
export interface ViseSpec {
  id: string
  name: string
  jaw_width_mm: number
  jaw_height_mm: number
  jaw_depth_mm: number
  max_opening_mm: number
  bolt_spacing_mm: number
  bolt_height_from_bottom_mm: number
  bolt_clearance_dia_mm: number
  bolt_cbore_dia_mm: number
  bolt_cbore_depth_mm: number
  default_jaw_gap_mm: number
}
export interface SoftjawRequest {
  vise_id: string
  op2_flip_axis: 'X' | 'Y'
  z_rotation_deg: 0 | 90
  grip_depth_mm: number
  xy_clearance_mm: number
  dogbone_radius_mm: number
  jaw_gap_mm: number | null
  jaw_material: 'aluminum_6082' | 'steel_c45'
}
export interface SoftjawCheck {
  code: string
  passed: boolean
  blocking: boolean
  value_mm: number | null
  limit_mm: number | null
  message: string
}
export interface SoftjawReport {
  job_id: string
  version: number
  passed: boolean
  checks: SoftjawCheck[]
  jaw_blank_mm: [number, number, number]
  part_envelope_mm: [number, number, number]
  cavity_depth_y_mm: number
  dogbones: [number, number][]
  part_offset_mm: [number, number, number]
  recommended_tool: { tool_id: string; tool_name: string; diameter_mm: number; flute_length_mm: number } | null
  urls?: Record<string, string>
}
export interface PartRevision {
  job_id: string
  filename: string
  revision: string | null
  status: string
  current_version: number
  created_at: number
}
export type ChangeCategory = 'BREAKING_CAM' | 'GEOMETRY_CHANGE' | 'METADATA_ONLY'
export interface FeatureDiffItem {
  category: ChangeCategory
  change: string
  scope: string
  feature_type: string | null
  id_old: string | null
  id_new: string | null
  face: string | null
  moved_mm: number | null
  fields: { path: string; before: unknown; after: unknown }[]
  message: string
}
export interface RevisionDiffReport {
  diff_id: string
  job_old_id: string
  job_new_id: string
  version_old: number
  version_new: number
  part_number: string | null
  revision_old: string | null
  revision_new: string | null
  overall: ChangeCategory | 'IDENTICAL'
  counts: Record<string, number>
  volumes: { old_mm3: number; new_mm3: number; added_mm3: number; removed_mm3: number; unchanged_mm3: number } | null
  items: FeatureDiffItem[]
  urls: Record<string, string>
}
export type MachineType = '3axis_vmc' | '5axis_3plus2'
export interface QuoteRequest {
  quantities: number[]
  custom_quantity: number | null
  machine_type: MachineType
  include_softjaws: boolean | null
  margin_pct: number | null
}
export interface QuoteLine { key: string; label: string; value: number; unit: string; formula: string }
export interface PriceBreak { quantity: number; unit_cost: number; unit_price: number; total_price: number; setup_share: number }
export interface MachineQuote {
  machine_type: MachineType
  label: string
  hourly_rate: number
  setups: number
  t_setup_min: number
  t_cycle_min: number
  t_load_unload_min: number
  lines: QuoteLine[]
  price_breaks: PriceBreak[]
}
export interface QuoteBreakdown {
  job_id: string
  version: number
  currency: string
  margin_pct: number
  material_group: string
  include_softjaws: boolean
  c_mat: number
  material_lines: QuoteLine[]
  machines: Record<MachineType, MachineQuote>
  selected: MachineType
  cheaper: MachineType | null
  notes: string[]
  exports: Record<'pdf' | 'csv', string>
}
export interface ShopRates {
  schema_version: string
  currency: string
  machines: Record<MachineType, { label: string; hourly_rate: number; tool_change_sec: number; load_unload_sec: number }>
  setup_minutes: { base_cam_programming: number; per_3axis_setup: number; per_5axis_setup: number; mill_softjaws_pair: number }
  materials: Record<string, { price_per_kg: number; mrr_cm3_per_min: number; finish_min_per_100cm2: number; hole_sec_base: number;
    tap_sec: number; ream_sec: number; chip_load_efficiency: number }>
  saw_waste_factor: number
  margin_pct_default: number
  fine_finish_factor: number
}