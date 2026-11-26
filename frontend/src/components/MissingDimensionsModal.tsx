import { useEffect, useMemo, useRef, useState } from 'react'
import { api, validationLines } from '../api/client'
import type { JobDetail, MissingDimension, PartSpec } from '../types/partSpec'

// DRAWING_SPEC.md §5: a job paused in `awaiting_dimensions` asks the operator for the values a sketch / DXF did not
// state. Answers are written into the PartSpec at `field_path` and the job is rebuilt (PATCH /spec, no AI).

type Json = Record<string, unknown> | unknown[]

/** Write `value` at a dotted path; `features.<id|index>` selects a feature by id or position. */
export function setAtPath(spec: PartSpec, path: string, value: number): PartSpec {
  const out = structuredClone(spec) as unknown as Record<string, unknown>
  const parts = path.split('.')
  let node: Json = out
  for (let i = 0; i < parts.length - 1; i++) {
    const key = parts[i]
    let next: unknown
    if (Array.isArray(node)) {
      next = /^\d+$/.test(key) ? node[Number(key)] : node.find((f) => (f as { id?: string })?.id === key)
    } else {
      if (node[key] == null) node[key] = {}
      next = node[key]
    }
    if (next == null || typeof next !== 'object') throw new Error(`veld ${path} bestaat niet in de PartSpec`)
    node = next as Json
  }
  const last = parts[parts.length - 1]
  if (Array.isArray(node)) throw new Error(`veld ${path} wijst naar een lijst`)
  node[last] = value
  if (last === 'depth' && 'through_all' in node) node.through_all = false // a depth means a blind cut
  return out as unknown as PartSpec
}

function parseNum(s: string): number | null {
  const v = Number(s.trim().replace(',', '.'))
  return Number.isFinite(v) && v > 0 ? v : null
}

interface Props {
  job: JobDetail
  onRebuilt: (d: JobDetail) => void
  /** large touch targets (mobile shell) */
  touch?: boolean
}

export default function MissingDimensionsModal({ job, onRebuilt, touch }: Props) {
  const spec = job.part_spec
  const missing: MissingDimension[] = useMemo(
    () => (spec?.missing_dimensions ?? []).filter((m) => m.blocking),
    [spec],
  )
  const waiting = job.status === 'awaiting_dimensions' && !!spec && missing.length > 0
  const [open, setOpen] = useState(true)
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string[] | null>(null)
  const first = useRef<HTMLInputElement>(null)

  useEffect(() => {
    setValues(Object.fromEntries(missing.map((m) => [m.field_path, m.suggested_default != null ? String(m.suggested_default) : ''])))
    setOpen(true)
    setError(null)
  }, [job.job_id, job.version, missing])

  useEffect(() => {
    if (waiting && open) first.current?.focus()
  }, [waiting, open])

  if (!waiting || !spec) return null

  const ready = missing.every((m) => parseNum(values[m.field_path] ?? '') != null)

  async function submit() {
    if (!spec) return
    setBusy(true)
    setError(null)
    try {
      let next = spec
      for (const m of missing) next = setAtPath(next, m.field_path, parseNum(values[m.field_path] ?? '')!)
      const answered = new Set(missing.map((m) => m.field_path))
      next = { ...next, missing_dimensions: (next.missing_dimensions ?? []).filter((m) => !answered.has(m.field_path)) }
      onRebuilt(await api.patchSpec(job.job_id, next))
    } catch (e) {
      setError(validationLines(e))
    } finally {
      setBusy(false)
    }
  }

  const banner = (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-600/60 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">
      <span className="font-medium">Wacht op {missing.length} maat/maten</span>
      <span className="text-amber-200/80">— de tekening of schets vermeldt ze niet; er wordt niets gegokt.</span>
      <button type="button" className={`btn btn-primary ml-auto ${touch ? 'min-h-11' : ''}`} onClick={() => setOpen(true)}>
        Maten invullen
      </button>
    </div>
  )

  if (!open) return banner

  return (
    <>
      {banner}
      <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 p-0 sm:items-center sm:p-4"
        onKeyDown={(e) => e.key === 'Escape' && setOpen(false)}>
        <form role="dialog" aria-modal="true" aria-labelledby="missing-title"
          className="max-h-[90dvh] w-full max-w-md overflow-y-auto rounded-t-2xl border border-slate-700 bg-slate-900 p-4 shadow-2xl sm:rounded-2xl"
          onSubmit={(e) => {
            e.preventDefault()
            if (ready && !busy) void submit()
          }}>
          <h2 id="missing-title" className="text-base font-semibold text-slate-100">Ontbrekende maten</h2>
          <p className="mt-1 text-xs text-slate-400">
            {job.filename}: deze waarden staan niet op de {spec.input_source === 'paper_sketch' ? 'schets' : 'tekening'}.
            Vul ze in (mm); daarna wordt het STEP-model meteen gebouwd, zonder AI.
          </p>
          <div className="mt-3 space-y-3">
            {missing.map((m, i) => (
              <label key={m.field_path} className="block">
                <span className="block text-sm text-slate-200">{m.prompt_nl}</span>
                <span className="block font-mono text-[11px] text-slate-500">{m.field_path}</span>
                <input ref={i === 0 ? first : undefined} className={`sel-input mt-1 w-full ${touch ? 'min-h-11 !text-base' : ''}`}
                  inputMode="decimal" autoComplete="off" placeholder="mm" value={values[m.field_path] ?? ''}
                  aria-invalid={values[m.field_path] !== '' && parseNum(values[m.field_path] ?? '') == null}
                  onChange={(e) => setValues({ ...values, [m.field_path]: e.target.value })} />
              </label>
            ))}
          </div>
          {error && (
            <ul className="mt-3 space-y-0.5 rounded-md border border-rose-800 bg-rose-500/10 p-2 text-xs text-rose-200">
              {error.map((l) => <li key={l}>{l}</li>)}
            </ul>
          )}
          <div className="mt-4 flex gap-2">
            <button type="button" className={`btn flex-1 justify-center ${touch ? 'min-h-11' : ''}`} onClick={() => setOpen(false)}>
              Later
            </button>
            <button type="submit" className={`btn btn-primary flex-1 justify-center ${touch ? 'min-h-11' : ''}`}
              disabled={!ready || busy}>
              {busy ? 'Bouwen…' : 'Bevestigen en bouwen'}
            </button>
          </div>
        </form>
      </div>
    </>
  )
}
