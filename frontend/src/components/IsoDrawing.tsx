import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { DrawingLanguage, JobDetail, SheetSize } from '../types/partSpec'
import { ISO_SCALES } from '../types/partSpec'

// EN ISO detail drawing (DRAWING_SPEC.md): open / download / regenerate with sheet, scale and language overrides.

const SHEET_LABEL: Record<SheetSize, string> = { AUTO: 'Automatisch', A4_LANDSCAPE: 'A4 liggend', A3_LANDSCAPE: 'A3 liggend' }
const LANG_LABEL: Record<DrawingLanguage, string> = { EN: 'Engels', EN_NL: 'Engels + Nederlands' }

export type DrawingSource = 'original' | 'iso'

/** Cache-bust an artifact URL after a regeneration (same path, new content). */
export function withTick(u: string | undefined, tick: number): string | undefined {
  return u ? `${u}${u.includes('?') ? '&' : '?'}t=${tick}` : undefined
}

export function DrawingSourceToggle({ value, onChange, isoAvailable, originalAvailable = true }: {
  value: DrawingSource
  onChange: (s: DrawingSource) => void
  isoAvailable: boolean
  originalAvailable?: boolean
}) {
  return (
    <div role="radiogroup" aria-label="Weergegeven tekening"
      className="inline-flex rounded-md border border-slate-700 bg-slate-900 p-0.5 normal-case tracking-normal">
      {(['original', 'iso'] as const).map((s) => (
        <button key={s} type="button" role="radio" aria-checked={value === s}
          disabled={s === 'iso' ? !isoAvailable : !originalAvailable}
          title={s === 'iso' && !isoAvailable ? 'Nog geen ISO-tekening voor deze versie'
            : s === 'original' && !originalAvailable ? 'Geen 2D-origineel (STEP-import)' : undefined}
          className={`rounded px-2 py-0.5 text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
            value === s ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}
          onClick={() => onChange(s)}>
          {s === 'original' ? 'Origineel' : 'ISO'}
        </button>
      ))}
    </div>
  )
}

interface Props {
  job: JobDetail
  running: boolean
  tick: number
  onGenerated: () => void
  /** popover: header split button (desktop). card: inline panel (mobile status tab). */
  variant?: 'popover' | 'card'
}

export default function IsoDrawing({ job, running, tick, onGenerated, variant = 'popover' }: Props) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const pdf = withTick(job.artifacts['drawing.pdf'], tick)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => !root.current?.contains(e.target as Node) && setOpen(false)
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (variant === 'card') {
    return (
      <section className="panel space-y-3 p-4">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="text-sm font-semibold text-slate-100">ISO-tekening</h3>
          <span className="text-[11px] text-slate-500">EN ISO 128 · 129-1 · 5456-2 · 7200</span>
        </div>
        <Body job={job} running={running} tick={tick} onGenerated={onGenerated} large />
      </section>
    )
  }

  return (
    <div ref={root} className="relative inline-flex">
      {pdf ? (
        <>
          <a className="btn rounded-r-none" href={pdf} target="_blank" rel="noreferrer"
            title={`ISO-tekening v${job.version} openen (PDF)`}>
            <DocIcon /> ISO v{job.version}
          </a>
          <button type="button" className="btn -ml-px rounded-l-none !px-1.5" aria-haspopup="dialog" aria-expanded={open}
            aria-label="Opties ISO-tekening" onClick={() => setOpen((o) => !o)}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.4}
              strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="m6 9 6 6 6-6" /></svg>
          </button>
        </>
      ) : (
        <button type="button" className="btn" aria-haspopup="dialog" aria-expanded={open}
          disabled={!job.artifacts['model.step']} onClick={() => setOpen((o) => !o)}
          title={job.artifacts['model.step'] ? 'EN ISO-werktekening maken' : 'Eerst een STEP-model nodig'}>
          <DocIcon /> ISO-tekening
        </button>
      )}
      {open && (
        <div role="dialog" aria-label="ISO-tekening"
          className="absolute right-0 top-full z-30 mt-1 w-80 rounded-lg border border-slate-700 bg-slate-900 p-3 shadow-xl">
          <Body job={job} running={running} tick={tick} onGenerated={onGenerated} />
        </div>
      )}
    </div>
  )
}

function Body({ job, running, tick, onGenerated, large }: Omit<Props, 'variant'> & { large?: boolean }) {
  const info = job.drawing
  const meta = job.part_spec?.drawing_metadata
  const art = job.artifacts
  const initial = () => ({
    sheet: (info?.overrides.sheet_size ?? meta?.sheet_size ?? 'AUTO') as SheetSize,
    scale: info?.overrides.scale ?? meta?.scale ?? 'AUTO',
    lang: (info?.overrides.language ?? meta?.language ?? 'EN') as DrawingLanguage,
    section: (info?.overrides.section_view ?? meta?.section_view ?? 'AUTO') !== 'NONE',
  })
  const [opts, setOpts] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // another job / version: start from its own settings
  useEffect(() => setOpts(initial()), [job.job_id, job.version, info?.version])

  const hasStep = !!art['model.step']
  const pdf = withTick(art['drawing.pdf'], tick)
  const pending = hasStep && !pdf && running
  const scales = ['AUTO', ...ISO_SCALES, ...(opts.scale !== 'AUTO' && !ISO_SCALES.includes(opts.scale as never) ? [opts.scale] : [])]

  const generate = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.generateDrawing(job.job_id, { sheet_size: opts.sheet, scale: opts.scale, language: opts.lang,
        section_view: opts.section ? 'AUTO' : 'NONE', version: job.version })
      onGenerated()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const sel = large ? 'sel-input min-h-11 w-full !text-sm' : 'sel-input w-full'
  const btn = large ? 'min-h-11 !text-sm' : ''
  const notes = [...(info?.warnings ?? []), ...(info?.balloons ?? []).map((b) => `ballon ${b}`)]

  return (
    <div className="space-y-3 text-xs text-slate-300">
      <p className="text-slate-400">
        {info && pdf ? (
          <>
            <span className="font-medium text-slate-200">{info.sheet_size.replace('_LANDSCAPE', '')} · {info.scale}</span>
            {' '}· v{info.version} · first-angle · ISO 2768-mK · <span className="text-amber-300">DRAFT</span> tot getekend/gecontroleerd
          </>
        ) : pending ? (
          'Tekening wordt gemaakt…'
        ) : hasStep ? (
          'Nog geen tekening voor deze versie.'
        ) : (
          'Eerst een STEP-model nodig.'
        )}
      </p>

      {pdf && (
        <div className="grid grid-cols-3 gap-1.5">
          <a className={`btn btn-primary justify-center ${btn}`} href={pdf} target="_blank" rel="noreferrer">PDF</a>
          <a className={`btn justify-center ${btn}`} href={withTick(`${art['drawing.svg']}&download=true`, tick)}>SVG</a>
          <a className={`btn justify-center ${btn}`} href={withTick(`${art['drawing.dxf']}&download=true`, tick)}>DXF</a>
        </div>
      )}

      <div className="grid grid-cols-2 gap-2">
        <label className="block">
          <span className="field-label">blad</span>
          <select className={sel} value={opts.sheet} onChange={(e) => setOpts({ ...opts, sheet: e.target.value as SheetSize })}>
            {(Object.keys(SHEET_LABEL) as SheetSize[]).map((s) => <option key={s} value={s}>{SHEET_LABEL[s]}</option>)}
          </select>
        </label>
        <label className="block">
          <span className="field-label">schaal (ISO 5455)</span>
          <select className={sel} value={opts.scale} onChange={(e) => setOpts({ ...opts, scale: e.target.value })}>
            {scales.map((s) => <option key={s} value={s}>{s === 'AUTO' ? 'Automatisch' : s}</option>)}
          </select>
        </label>
        <label className="col-span-2 flex items-center gap-2 text-slate-300">
          <input type="checkbox" className={large ? 'h-5 w-5' : ''} checked={opts.section}
            onChange={(e) => setOpts({ ...opts, section: e.target.checked })} />
          Doorsnede A-A (gearceerd, EN ISO 128-50) waar er verborgen vormen zijn
        </label>
        <label className="col-span-2 block">
          <span className="field-label">taal titelblok</span>
          <select className={sel} value={opts.lang} onChange={(e) => setOpts({ ...opts, lang: e.target.value as DrawingLanguage })}>
            {(Object.keys(LANG_LABEL) as DrawingLanguage[]).map((l) => <option key={l} value={l}>{LANG_LABEL[l]}</option>)}
          </select>
        </label>
      </div>

      <button type="button" className={`btn w-full justify-center ${pdf ? '' : 'btn-primary'} ${btn}`}
        disabled={!hasStep || busy} onClick={() => void generate()}>
        {busy ? 'Bezig…' : pdf ? 'Opnieuw genereren' : 'Tekening genereren'}
      </button>

      {notes.length > 0 && (
        <ul className="space-y-0.5 rounded-md border border-amber-700/50 bg-amber-500/10 p-2 text-[11px] text-amber-200">
          {notes.map((n) => <li key={n}>{n}</li>)}
        </ul>
      )}
      {error && <p className="rounded-md border border-rose-800 bg-rose-500/10 p-2 text-rose-200">{error}</p>}
    </div>
  )
}

function DocIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6" />
    </svg>
  )
}
