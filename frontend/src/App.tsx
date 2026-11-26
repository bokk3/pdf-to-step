import { useCallback, useEffect, useRef, useState } from 'react'
import { api, useJobEvents, type Health } from './api/client'
import DfmWarningsPanel from './components/DfmWarningsPanel'
import FeatureEditor from './components/FeatureEditor'
import IsoDrawing, { DrawingSourceToggle, withTick, type DrawingSource } from './components/IsoDrawing'
import JobList from './components/JobList'
import MissingDimensionsModal from './components/MissingDimensionsModal'
import StepImportNote from './components/StepImportNote'
import MobileShell from './components/MobileShell'
import PdfViewer from './components/PdfViewer'
import PipelineProgress, { STATUS_LABEL } from './components/PipelineProgress'
import Step3DViewer from './components/Step3DViewer'
import UploadDropzone from './components/UploadDropzone'
import { MOBILE_QUERY, useMediaQuery } from './hooks/useMediaQuery'
import type { JobDetail, JobSummary, PipelineEvent } from './types/partSpec'
import { TERMINAL } from './types/partSpec'

const SOURCE_LABEL: Record<string, string> = {
  extraction: 'AI-extractie',
  reextract: 'AI-herextractie',
  self_heal: 'zelfherstel',
  critic: 'visuele correctie',
  complex_code: 'fallbackcode',
  user_edit: 'operator',
  dxf: 'DXF-import',
  step: 'STEP-import',
  sketch: 'schets',
}

function readHashJob(): string | null {
  const m = window.location.hash.match(/job=([a-f0-9]+)/)
  return m ? m[1] : null
}

export default function App() {
  const mobile = useMediaQuery(MOBILE_QUERY)
  const [health, setHealth] = useState<Health | null>(null)
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [jobId, setJobId] = useState<string | null>(readHashJob())
  const [viewVersion, setViewVersion] = useState<number | null>(null)
  const [job, setJob] = useState<JobDetail | null>(null)
  const [selectedFeature, setSelectedFeature] = useState<string | null>(null)
  const [epoch, setEpoch] = useState(0)
  const [renderTick, setRenderTick] = useState(0)
  const [drawingSource, setDrawingSource] = useState<DrawingSource>('original')
  const refreshTimer = useRef<number | undefined>(undefined)

  const loadJobs = useCallback(() => api.listJobs().then(setJobs).catch(() => undefined), [])
  const loadJob = useCallback(async () => {
    if (!jobId) return setJob(null)
    try {
      setJob(await api.getJob(jobId, viewVersion))
    } catch {
      setJob(null)
    }
  }, [jobId, viewVersion])

  useEffect(() => {
    api.health(true).then(setHealth).catch(() => setHealth(null))
    void loadJobs()
  }, [loadJobs])

  useEffect(() => {
    const busy = jobs.some((j) => j.running || !TERMINAL.includes(j.status))
    const t = window.setInterval(loadJobs, busy ? 3000 : 15000)
    return () => window.clearInterval(t)
  }, [jobs, loadJobs])

  useEffect(() => {
    window.location.hash = jobId ? `job=${jobId}` : ''
    setSelectedFeature(null)
  }, [jobId])

  useEffect(() => { // shared links / back button: #job=<id> selects that job without a reload
    const onHash = () => {
      const id = readHashJob()
      if (id) setJobId((cur) => (cur === id ? cur : id))
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    void loadJob()
  }, [loadJob])

  const scheduleRefresh = useCallback(() => {
    window.clearTimeout(refreshTimer.current)
    refreshTimer.current = window.setTimeout(() => {
      void loadJob()
      void loadJobs()
    }, 250)
  }, [loadJob, loadJobs])

  const onEvent = useCallback(
    (e: PipelineEvent) => {
      const d = e.data as { renders?: boolean; drawing?: unknown } | null
      if (d && (d.renders || d.drawing)) setRenderTick((t) => t + 1) // deferred ortho render / regenerated drawing
      scheduleRefresh()
    },
    [scheduleRefresh],
  )
  const { events, live } = useJobEvents(jobId, epoch, onEvent, scheduleRefresh)

  const running = !!job && (job.running || !TERMINAL.includes(job.status))
  const spec = job?.part_spec ?? null
  const report = job?.dfm_report ?? null
  const art = job?.artifacts ?? {}
  const g = report?.geometry
  const bboxMax = g ? (g.bbox_max_mm.map((v, i) => v - g.bbox_min_mm[i]) as [number, number, number]) : undefined
  const bust = (u?: string) => withTick(u, renderTick)
  const isoUrl = art['drawing.pdf']
  const originalUrl = art['original.pdf']
  const shown = !originalUrl ? 'iso' : isoUrl ? drawingSource : 'original'
  const pdfUrl = shown === 'iso' && isoUrl ? bust(isoUrl) : originalUrl
  const sourceToggle = (
    <DrawingSourceToggle value={shown} onChange={setDrawingSource} isoAvailable={!!isoUrl} originalAvailable={!!originalUrl} />
  )
  const onDrawing = () => {
    setRenderTick((t) => t + 1)
    setDrawingSource('iso')
    setEpoch((e) => e + 1) // resubscribe: the stream of a finished job has closed, replay shows the new event
    void loadJob()
  }

  const selectJob = (id: string) => {
    setViewVersion(null)
    setJobId(id)
  }
  const onUploaded = (id: string) => {
    setViewVersion(null)
    setJobId(id)
    setEpoch((e) => e + 1)
    void loadJobs()
  }
  const onRebuilt = (detail: JobDetail) => {
    setViewVersion(null)
    setJob(detail)
    setEpoch((e) => e + 1) // resubscribe: log + deferred ortho render event
    void loadJobs()
  }
  const onReextract = () => {
    setViewVersion(null)
    setEpoch((e) => e + 1)
    scheduleRefresh()
  }
  const onDelete = async () => {
    if (!job || !confirm(`Job ${job.filename} en alle bestanden verwijderen?`)) return
    await api.deleteJob(job.job_id)
    setJobId(null)
    void loadJobs()
  }

  if (mobile) {
    return (
      <MobileShell
        health={health} jobs={jobs} job={job} events={events} live={live} running={running}
        viewVersion={viewVersion} setViewVersion={setViewVersion}
        selectedFeature={selectedFeature} setSelectedFeature={setSelectedFeature}
        orthoUrl={bust(art['ortho_check.png'])} bboxMax={bboxMax} floorZ={g?.bbox_min_mm[2] ?? 0}
        onSelectJob={selectJob} onUploaded={onUploaded} onDelete={() => void onDelete()}
        onRebuilt={onRebuilt} onReextract={onReextract}
        renderTick={renderTick} pdfUrl={pdfUrl} sourceToggle={sourceToggle} onDrawing={onDrawing}
      />
    )
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-slate-800 bg-slate-950 px-4 py-2">
        <img src="/icon.svg" alt="" className="h-7 w-7 rounded-md" />
        <div className="min-w-0">
          <h1 className="text-sm font-semibold text-slate-100">PDF → STEP · CNC-generator</h1>
          <p className="truncate text-[11px] text-slate-500">2D-tekening → PartSpec → native B-Rep (build123d / OCCT) → STEP {health?.step_schema ?? 'AP214'}</p>
        </div>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-1.5 text-[11px]">
          <Chip ok={!!health?.occt?.ok} label={health?.occt?.ok ? `OCCT · build123d ${health.occt.build123d}` : 'OCCT ?'} />
          <Chip
            ok={!!health && !health.deepseek.mock && health.deepseek.reachable !== false}
            warn={!!health?.deepseek.mock}
            label={!health ? 'API offline' : health.deepseek.mock ? 'DeepSeek niet geconfigureerd (offline)' : `DeepSeek · ${health.deepseek.vision_model} / ${health.deepseek.code_model}`}
          />
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-72 flex-col gap-3 border-r border-slate-800 p-3">
          <UploadDropzone onUploaded={onUploaded} />
          <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
            <JobList jobs={jobs} selectedId={jobId} onSelect={selectJob} />
          </div>
        </aside>

        <main className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
          {!job ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-slate-500">
              <p className="text-lg text-slate-300">Upload een 2D-werktekening om te beginnen</p>
              <p className="max-w-lg text-sm">
                Tekstlaag, vectoren en 300 DPI-tegels gaan naar het vision-model, dat een getypeerde PartSpec invult. Een
                deterministische build123d-compiler maakt daar een native B-Rep STEP van; de verifier controleert geometrie en
                maakbaarheid.
              </p>
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="truncate text-base font-semibold text-slate-100">{job.filename}</h2>
                <span className={`rounded px-1.5 py-0.5 text-[11px] ${running ? 'bg-sky-500/15 text-sky-200' : job.status === 'ready' ? 'bg-emerald-500/15 text-emerald-300' : job.status === 'awaiting_dimensions' ? 'bg-amber-500/15 text-amber-200' : 'bg-rose-500/15 text-rose-300'}`}>
                  {STATUS_LABEL[job.status] ?? job.status}
                </span>
                {spec?.part_name && <span className="font-mono text-xs text-slate-400">{spec.part_name}</span>}
                <div className="ml-auto flex flex-wrap items-center gap-1.5">
                  {job.versions.length > 0 && (
                    <select className="sel-input" value={viewVersion ?? ''} onChange={(e) => setViewVersion(e.target.value === '' ? null : Number(e.target.value))}>
                      <option value="">actueel (v{job.current_version})</option>
                      {[...job.versions].reverse().map((v) => (
                        <option key={v.version} value={v.version}>v{v.version} · {SOURCE_LABEL[v.source] ?? v.source}</option>
                      ))}
                    </select>
                  )}
                  <IsoDrawing job={job} running={running} tick={renderTick} onGenerated={onDrawing} />
                  {art['model.step'] && (
                    <a className="btn btn-primary" href={art['model.step']}>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2}
                        strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                        <path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14" />
                      </svg>
                      STEP v{job.version}
                    </a>
                  )}
                  <button className="btn btn-danger" disabled={running} onClick={() => void onDelete()}>
                    Verwijderen
                  </button>
                </div>
              </div>

              {job.status === 'failed' && job.error && (
                <div className="rounded-md border border-rose-800 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">{job.error}</div>
              )}
              <MissingDimensionsModal job={job} onRebuilt={onRebuilt} />
              {job.step_import && <StepImportNote complete={job.step_import.complete} />}

              <PipelineProgress job={job} events={events} live={live} iterations={job.iteration_logs} />

              <div className="grid gap-3 xl:grid-cols-2">
                <div className="h-[55vh] min-h-[320px]">
                  {pdfUrl ? (
                    <PdfViewer url={pdfUrl} header={sourceToggle} />
                  ) : (
                    <section className="panel flex h-full items-center justify-center p-4 text-sm text-slate-500">
                      {running ? 'De ISO-tekening wordt gemaakt…' : 'Geen tekening voor deze versie.'}
                    </section>
                  )}
                </div>
                <div className="h-[55vh] min-h-[320px]">
                  <Step3DViewer glbUrl={art['preview.glb']} orthoUrl={bust(art['ortho_check.png'])} bboxMax={bboxMax}
                    floorZ={g?.bbox_min_mm[2] ?? 0} />
                </div>
              </div>

              <div className="grid gap-3 xl:grid-cols-3">
                <div className="min-h-[320px] xl:col-span-2">
                  {spec ? (
                    <FeatureEditor
                      jobId={job.job_id}
                      spec={spec}
                      version={job.version}
                      report={report}
                      selectedId={selectedFeature}
                      onSelect={setSelectedFeature}
                      busy={running}
                      onRebuilt={onRebuilt}
                      onReextract={onReextract}
                    />
                  ) : (
                    <section className="panel p-3 text-sm text-slate-500">De PartSpec verschijnt na de extractie.</section>
                  )}
                </div>
                <div className="max-h-[85vh] min-h-[320px]">
                  <DfmWarningsPanel
                    report={report}
                    ambiguities={spec?.ambiguities ?? []}
                    critic={viewVersion === null ? job.critic : null}
                    selectedFeature={selectedFeature}
                    onSelectFeature={setSelectedFeature}
                  />
                </div>
              </div>
            </>
          )}
        </main>
      </div>
    </div>
  )
}

function Chip({ ok, warn, label }: { ok: boolean; warn?: boolean; label: string }) {
  const cls = ok ? 'border-emerald-800 text-emerald-300' : warn ? 'border-amber-700 text-amber-200' : 'border-rose-800 text-rose-300'
  return <span className={`rounded-full border px-2 py-0.5 ${cls}`}>{label}</span>
}
