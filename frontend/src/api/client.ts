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

export const api = {
  health: (deep = false) => request<Health>(`/api/health${deep ? '?deep=true' : ''}`),
  listJobs: () => request<JobSummary[]>('/api/jobs'),
  getJob: (id: string, version?: number | null) =>
    request<JobDetail>(`/api/jobs/${id}${version ? `?version=${version}` : ''}`),
  deleteJob: (id: string) => request<void>(`/api/jobs/${id}`, { method: 'DELETE' }),
  upload: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ job_id: string; status: string; filename: string }>('/api/jobs', { method: 'POST', body: fd })
  },
  /** §7 PATCH /spec: deterministic rebuild, no AI call; returns the new version's state. */
  patchSpec: (id: string, spec: PartSpec) =>
    request<JobDetail>(`/api/jobs/${id}/spec`, { method: 'PATCH', body: JSON.stringify(spec) }),
  /** DRAWING_SPEC §6: (re)generate the EN ISO drawing of the current version, optional overrides. */
  generateDrawing: (
    id: string,
    overrides: { sheet_size?: SheetSize; scale?: string; language?: DrawingLanguage; version?: number } = {},
  ) =>
    request<DrawingInfo & { artifacts: Record<string, string> }>(`/api/jobs/${id}/drawing`, {
      method: 'POST',
      body: JSON.stringify(overrides),
    }),
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
