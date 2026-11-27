import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Health } from '../api/client'
import type { JobDetail, JobSummary, PipelineEvent } from '../types/partSpec'
import DfmWarningsPanel from './DfmWarningsPanel'
import FeatureEditor from './FeatureEditor'
import IsoDrawing from './IsoDrawing'
import VerifyButton from './VerifyButton'
import MissingDimensionsModal from './MissingDimensionsModal'
import StepImportNote from './StepImportNote'
import JobList from './JobList'
import PdfViewer from './PdfViewer'
import PipelineProgress, { STATUS_LABEL } from './PipelineProgress'
import Step3DViewer from './Step3DViewer'
import UploadDropzone from './UploadDropzone'

type Tab = 'status' | 'drawing' | 'model' | 'spec' | 'check'
const TAB_SLUG: Record<Tab, string> = { status: 'status', drawing: 'tekening', model: '3d', spec: 'spec', check: 'controle' }

function readHashTab(): Tab {
  const m = window.location.hash.match(/tab=([a-z0-9]+)/)
  const hit = m && (Object.entries(TAB_SLUG) as [Tab, string][]).find(([, s]) => s === m[1])
  return hit ? hit[0] : 'status'
}

const SOURCE_LABEL: Record<string, string> = {
  extraction: 'AI-extractie', reextract: 'AI-herextractie', self_heal: 'zelfherstel', critic: 'visuele correctie',
  complex_code: 'fallbackcode', user_edit: 'operator', dxf: 'DXF-import', step: 'STEP-import', sketch: 'schets',
}

interface Props {
  health: Health | null
  jobs: JobSummary[]
  job: JobDetail | null
  events: PipelineEvent[]
  live: boolean
  running: boolean
  viewVersion: number | null
  setViewVersion: (v: number | null) => void
  selectedFeature: string | null
  setSelectedFeature: (id: string | null) => void
  orthoUrl?: string
  bboxMax?: [number, number, number]
  floorZ: number
  onSelectJob: (id: string) => void
  onUploaded: (id: string) => void
  onDelete: () => void
  onRebuilt: (d: JobDetail) => void
  onReextract: () => void
  onReload: () => void
  renderTick: number
  pdfUrl?: string
  sourceToggle: ReactNode
  onDrawing: () => void
}

/** Phone / small-tablet layout: compact top bar, job list as its own screen, one full-height tab at a time. */
export default function MobileShell(p: Props) {
  const { job } = p
  const [tab, setTab] = useState<Tab>(readHashTab)
  const [jobsOpen, setJobsOpen] = useState(false)
  const firstJob = useRef(job?.job_id ?? window.location.hash.match(/job=([a-f0-9]+)/)?.[1])

  // a different job starts on the status tab (the initial one keeps a deep-linked #…&tab=)
  useEffect(() => {
    if (job?.job_id && job.job_id !== firstJob.current) {
      firstJob.current = job.job_id
      setTab('status')
    }
  }, [job?.job_id])

  // deep link: #job=<id>&tab=<tab> survives reloads and can be shared
  useEffect(() => {
    if (!job) return
    const hash = `#job=${job.job_id}${tab === 'status' ? '' : `&tab=${TAB_SLUG[tab]}`}`
    if (window.location.hash !== hash) history.replaceState(null, '', hash)
  }, [job, tab])

  if (!job || jobsOpen) {
    return (
      <HomeScreen
        {...p}
        onClose={job ? () => setJobsOpen(false) : undefined}
        onPick={(id) => {
          setJobsOpen(false)
          p.onSelectJob(id)
        }}
        onUploaded={(id) => {
          setJobsOpen(false)
          p.onUploaded(id)
        }}
      />
    )
  }

  const report = job.dfm_report
  const spec = job.part_spec
  const art = job.artifacts
  const errors = report?.issues.filter((i) => i.severity === 'error').length ?? 0
  const dfmWarnings = report?.issues.filter((i) => i.severity === 'warning').length ?? 0
  const questions = spec?.ambiguities.length ?? 0
  const warnings = dfmWarnings + questions
  const review = report?.review_items.length ?? 0
  const status = p.running ? STATUS_LABEL[job.status] ?? job.status : STATUS_LABEL[job.status]

  const openFeature = (id: string) => {
    p.setSelectedFeature(id)
    setTab('spec')
  }

  return (
    <div className="flex h-dvh flex-col bg-slate-950">
      {/* top bar */}
      <header className="pt-safe shrink-0 border-b border-slate-800 bg-slate-950/95 backdrop-blur">
        <div className="flex h-14 items-center gap-2 px-2">
          <button className="btn !border-transparent !bg-transparent" aria-label="Alle jobs" onClick={() => setJobsOpen(true)}>
            <Icon d="M4 6h16M4 12h16M4 18h10" />
          </button>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[15px] font-semibold text-slate-100">{job.filename}</div>
            <div className="flex items-center gap-1.5 truncate text-xs text-slate-400">
              <StatusDot status={job.status} running={p.running} />
              {status}
              {job.version > 0 && <span>· v{job.version}</span>}
              {spec?.part_name && <span className="truncate">· {spec.part_name}</span>}
            </div>
          </div>
          {art['model.step'] && (
            <a className="btn btn-primary !px-3" href={art['model.step']} aria-label={`STEP v${job.version} downloaden`}>
              <Icon d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14" />
            </a>
          )}
        </div>
      </header>

      {/* content */}
      <main className="min-h-0 flex-1">
        {tab === 'status' && (
          <div className="scroll-thin h-full space-y-3 overflow-y-auto overscroll-contain p-3">
            <section className="panel space-y-3 p-4">
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="break-all text-base font-semibold text-slate-100">{job.filename}</div>
                  {spec?.part_name && <div className="mt-0.5 break-all font-mono text-xs text-slate-400">{spec.part_name}</div>}
                  {spec?.material && <div className="text-xs text-slate-400">{spec.material}</div>}
                </div>
                <StatusBadge status={job.status} running={p.running} />
              </div>
              {art['model.step'] ? (
                <a className="btn btn-primary min-h-12 w-full !text-base font-semibold" href={art['model.step']}>
                  <Icon d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14" /> STEP downloaden · v{job.version}
                </a>
              ) : (
                <div className="rounded-md border border-dashed border-slate-700 p-3 text-center text-sm text-slate-400">
                  {p.running ? 'STEP wordt gemaakt…' : 'Nog geen STEP voor deze versie'}
                </div>
              )}
              <div className="grid grid-cols-2 gap-2">
                <Tile label="Te controleren" sub="zekerheid < 85%" value={review} tone={review ? 'amber' : 'ok'} onClick={() => setTab('spec')} />
                {errors ? (
                  <Tile label="Fouten" sub="geometrie / bouw" value={errors} tone="red" onClick={() => setTab('check')} />
                ) : (
                  <Tile label="Aandachtspunten" sub={`${dfmWarnings} DFM · ${questions} AI-vragen`} value={warnings}
                    tone={warnings ? 'amber' : 'ok'} onClick={() => setTab('check')} />
                )}
              </div>
              {job.versions.length > 0 && (
                <label className="block">
                  <span className="field-label">versie</span>
                  <select className="sel-input w-full" value={p.viewVersion ?? ''}
                    onChange={(e) => p.setViewVersion(e.target.value === '' ? null : Number(e.target.value))}>
                    <option value="">actueel (v{job.current_version})</option>
                    {[...job.versions].reverse().map((v) => (
                      <option key={v.version} value={v.version}>v{v.version} · {SOURCE_LABEL[v.source] ?? v.source}</option>
                    ))}
                  </select>
                </label>
              )}
            </section>

            {job.status === 'failed' && job.error && (
              <div className="rounded-lg border border-rose-800 bg-rose-500/10 p-3 text-sm text-rose-200">{job.error}</div>
            )}
            <MissingDimensionsModal job={job} onRebuilt={p.onRebuilt} touch />
            {job.step_import && <StepImportNote complete={job.step_import.complete} />}

            {(art['model.step'] || art['drawing.pdf']) && (
              <IsoDrawing variant="card" job={job} running={p.running} tick={p.renderTick} onGenerated={p.onDrawing} />
            )}

            {art['model.step'] && (
              <div className="grid grid-cols-2 gap-2">
                {art['setup_sheet.pdf'] && <a className="btn" href={art['setup_sheet.pdf']} target="_blank" rel="noreferrer">Instelblad</a>}
                <a className="btn" href={`/shopfloor/${job.job_id}`}>Werkplaatsweergave</a>
                <div className="col-span-2 grid"><VerifyButton job={job} running={p.running} onDone={p.onReload} /></div>
              </div>
            )}

            <PipelineProgress job={job} events={p.events} live={p.live} iterations={job.iteration_logs} compact />

            <button className="btn btn-danger w-full" disabled={p.running} onClick={p.onDelete}>Job verwijderen</button>
          </div>
        )}
        {tab === 'drawing' && (
          <div className="h-full p-2">
            {p.pdfUrl ? <PdfViewer url={p.pdfUrl} header={p.sourceToggle} />
              : <Empty>{p.running ? 'De ISO-tekening wordt gemaakt…' : 'Geen tekening voor deze versie.'}</Empty>}
          </div>
        )}
        {tab === 'model' && (
          <div className="h-full p-2">
            <Step3DViewer glbUrl={art['preview.glb']} orthoUrl={p.orthoUrl} bboxMax={p.bboxMax} floorZ={p.floorZ} />
          </div>
        )}
        {tab === 'spec' && (
          <div className="h-full p-2">
            {spec ? (
              <FeatureEditor jobId={job.job_id} spec={spec} version={job.version} report={report}
                selectedId={p.selectedFeature} onSelect={p.setSelectedFeature} busy={p.running}
                onRebuilt={p.onRebuilt} onReextract={() => { setTab('status'); p.onReextract() }} />
            ) : (
              <Empty>De PartSpec verschijnt na de extractie.</Empty>
            )}
          </div>
        )}
        {tab === 'check' && (
          <div className="h-full p-2">
            <DfmWarningsPanel report={report} ambiguities={spec?.ambiguities ?? []} critic={p.viewVersion === null ? job.critic : null}
              selectedFeature={p.selectedFeature} onSelectFeature={openFeature} />
          </div>
        )}
      </main>

      {/* bottom tab bar */}
      <nav className="pb-safe shrink-0 border-t border-slate-800 bg-slate-950/95 backdrop-blur" aria-label="Weergaven">
        <div className="grid h-16 grid-cols-5">
          <TabButton active={tab === 'status'} onClick={() => setTab('status')} label="Status"
            icon="M3 12h4l3-8 4 16 3-8h4"
            badge={p.running ? <span className="h-2 w-2 animate-pulse rounded-full bg-sky-400" /> : job.status === 'failed' ? <span className="h-2 w-2 rounded-full bg-rose-500" /> : null} />
          <TabButton active={tab === 'drawing'} onClick={() => setTab('drawing')} label="Tekening"
            icon="M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6" />
          <TabButton active={tab === 'model'} onClick={() => setTab('model')} label="3D"
            icon="M12 3 4 7.5v9L12 21l8-4.5v-9zM4 7.5 12 12l8-4.5M12 12v9" />
          <TabButton active={tab === 'spec'} onClick={() => setTab('spec')} label="Spec"
            icon="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4"
            badge={review ? <Count n={review} tone="amber" /> : null} />
          <TabButton active={tab === 'check'} onClick={() => setTab('check')} label="Controle"
            icon="M12 3 5 6v6c0 4 3 7.5 7 9 4-1.5 7-5 7-9V6zM9 12l2 2 4-4"
            badge={errors ? <Count n={errors} tone="red" /> : warnings ? <Count n={warnings} tone="amber" /> : null} />
        </div>
      </nav>
    </div>
  )
}

// ---- home / job list screen ----------------------------------------------------------------

function HomeScreen(p: Props & { onClose?: () => void; onPick: (id: string) => void }) {
  const h = p.health
  return (
    <div className="flex h-dvh flex-col bg-slate-950">
      <header className="pt-safe shrink-0 border-b border-slate-800">
        <div className="flex h-14 items-center gap-3 px-3">
          <img src="/icon.svg" alt="" className="h-8 w-8 rounded-lg" />
          <div className="min-w-0 flex-1">
            <div className="text-[15px] font-semibold text-slate-100">PDF → STEP</div>
            <div className="truncate text-xs text-slate-400">CNC-generator · native B-Rep</div>
          </div>
          {p.onClose && (
            <button className="btn" onClick={p.onClose} aria-label="Terug naar de job">
              <Icon d="M6 6l12 12M18 6 6 18" />
            </button>
          )}
        </div>
      </header>
      <div className="scroll-thin pb-safe min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain p-3">
        <UploadDropzone onUploaded={p.onUploaded} />
        <section>
          <h2 className="field-label px-1">Recente jobs</h2>
          <JobList jobs={p.jobs} selectedId={p.job?.job_id ?? null} onSelect={p.onPick} large />
        </section>
        <section className="space-y-1.5 px-1 text-xs text-slate-400">
          <h2 className="field-label">Systeem</h2>
          <HealthLine ok={!!h?.occt?.ok} label={h?.occt?.ok ? `CAD-kernel OCCT · build123d ${h.occt.build123d}` : 'CAD-kernel: onbekend'} />
          <HealthLine
            ok={!!h && !h.deepseek.mock && h.deepseek.reachable !== false}
            warn={!!h?.deepseek.mock}
            label={!h ? 'Server niet bereikbaar' : h.deepseek.mock ? 'DeepSeek niet geconfigureerd' : `DeepSeek · ${h.deepseek.vision_model} / ${h.deepseek.code_model}`}
          />
        </section>
      </div>
    </div>
  )
}

// ---- small pieces ----------------------------------------------------------------------------

function Icon({ d, size = 22 }: { d: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d={d} />
    </svg>
  )
}

function TabButton({ active, onClick, label, icon, badge }: { active: boolean; onClick: () => void; label: string; icon: string; badge?: ReactNode }) {
  return (
    <button onClick={onClick} aria-current={active ? 'page' : undefined}
      className={`relative flex flex-col items-center justify-center gap-0.5 text-[11px] font-medium ${
        active ? 'text-sky-300' : 'text-slate-400 active:text-slate-200'
      }`}>
      {active && <span className="absolute top-0 h-0.5 w-8 rounded-full bg-sky-400" aria-hidden />}
      <span className="relative">
        <Icon d={icon} />
        {badge && <span className="absolute -top-1.5 -right-2.5 flex">{badge}</span>}
      </span>
      {label}
    </button>
  )
}

function Count({ n, tone }: { n: number; tone: 'amber' | 'red' }) {
  return (
    <span className={`min-w-[18px] rounded-full px-1 text-center text-[10px] font-bold leading-[18px] text-slate-950 ${tone === 'red' ? 'bg-rose-500 text-white' : 'bg-amber-400'}`}>
      {n > 99 ? '99+' : n}
    </span>
  )
}

function Tile({ label, sub, value, tone, onClick }: { label: string; sub?: string; value: number; tone: 'ok' | 'amber' | 'red'; onClick: () => void }) {
  const cls = { ok: 'border-emerald-900 text-emerald-300', amber: 'border-amber-800 text-amber-200', red: 'border-rose-800 text-rose-200' }[tone]
  return (
    <button onClick={onClick} className={`rounded-lg border bg-slate-950/50 px-3 py-2.5 text-left active:bg-slate-900 ${cls}`}>
      <div className="text-2xl font-semibold leading-none">{value}</div>
      <div className="mt-1 text-xs font-medium text-slate-300">{label}</div>
      {sub && <div className="text-[11px] text-slate-400">{sub}</div>}
    </button>
  )
}

function StatusDot({ status, running }: { status: string; running: boolean }) {
  const cls = running ? 'animate-pulse bg-sky-400' : status === 'ready' ? 'bg-emerald-400' : status === 'failed' ? 'bg-rose-500'
    : status === 'awaiting_dimensions' ? 'bg-amber-400' : 'bg-slate-500'
  return <span className={`h-2 w-2 shrink-0 rounded-full ${cls}`} />
}

function StatusBadge({ status, running }: { status: string; running: boolean }) {
  const cls = running ? 'bg-sky-500/15 text-sky-200' : status === 'ready' ? 'bg-emerald-500/15 text-emerald-300'
    : status === 'awaiting_dimensions' ? 'bg-amber-500/15 text-amber-200' : 'bg-rose-500/15 text-rose-300'
  return <span className={`shrink-0 rounded-md px-2 py-1 text-xs font-medium ${cls}`}>{STATUS_LABEL[status] ?? status}</span>
}

function HealthLine({ ok, warn, label }: { ok: boolean; warn?: boolean; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className={`h-2 w-2 rounded-full ${ok ? 'bg-emerald-400' : warn ? 'bg-amber-400' : 'bg-rose-500'}`} />
      {label}
    </div>
  )
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="panel flex h-full items-center justify-center p-6 text-center text-sm text-slate-400">{children}</div>
}
