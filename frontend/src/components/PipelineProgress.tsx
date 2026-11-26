import { useEffect, useRef } from 'react'
import type { IterationLog, JobSummary, PipelineEvent } from '../types/partSpec'

// §7 SSE stages
const STEPS: { stage: string; label: string }[] = [
  { stage: 'parsing_pdf', label: 'PDF ontleden' },
  { stage: 'extracting_features', label: 'Features extraheren' },
  { stage: 'compiling_cad', label: 'CAD compileren' },
  { stage: 'verifying', label: 'Verifiëren' },
  { stage: 'retrying', label: 'Zelfherstel' },
  { stage: 'ready', label: 'STEP klaar' },
]

export const STATUS_LABEL: Record<string, string> = {
  queued: 'In wachtrij',
  parsing_pdf: 'PDF ontleden',
  extracting_features: 'Features extraheren',
  awaiting_dimensions: 'Wacht op maten',
  compiling_cad: 'CAD compileren',
  verifying: 'Verifiëren',
  retrying: 'Zelfherstel',
  ready: 'Klaar',
  failed: 'Mislukt',
}

const LEVEL_CLS: Record<string, string> = { info: 'text-slate-300', warning: 'text-amber-300', error: 'text-rose-400' }

type StepState = 'active' | 'complete' | 'done' | 'failed' | 'skipped' | 'pending'

function stepStates(job: JobSummary, events: PipelineEvent[]): StepState[] {
  const finished = job.status === 'ready' || job.status === 'failed'
  const activeIdx = STEPS.findIndex((s) => s.stage === job.status)
  const visited = new Set(events.map((e) => e.stage))
  return STEPS.map((s, i) => {
    if (i === STEPS.length - 1) return job.status === 'ready' ? 'done' : job.status === 'failed' ? 'failed' : 'pending'
    if (finished) return visited.has(s.stage) ? 'complete' : 'skipped'
    return i === activeIdx ? 'active' : visited.has(s.stage) ? 'complete' : 'pending'
  })
}

interface Props {
  job: JobSummary
  events: PipelineEvent[]
  live: boolean
  iterations: IterationLog[]
  /** compact = vertical stepper + collapsible log (mobile) */
  compact?: boolean
}

export default function PipelineProgress({ job, events, live, iterations, compact }: Props) {
  const logRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [events.length])

  const states = stepStates(job, events)
  const bar = job.status === 'failed' ? 'bg-rose-500' : job.status === 'ready' ? 'bg-emerald-500'
    : job.status === 'awaiting_dimensions' ? 'bg-amber-500' : 'bg-sky-500'
  const aiIterations = iterations.filter((i) => i.iteration !== 'operator')

  const log = (
    <div ref={logRef} className="scroll-thin max-h-48 overflow-y-auto rounded-md bg-slate-950/70 p-2 font-mono text-[11px] leading-relaxed [@media(pointer:coarse)]:text-xs">
      {events.length === 0 && <div className="text-slate-500">Nog geen gebeurtenissen</div>}
      {events.map((e) => (
        <div key={e.id} className={LEVEL_CLS[e.level] ?? 'text-slate-300'}>
          <span className="text-slate-500">{new Date(e.ts * 1000).toLocaleTimeString('nl-NL')} </span>
          {!compact && <span className="text-slate-500">[{e.stage}] </span>}
          {e.message}
        </div>
      ))}
    </div>
  )

  if (compact) {
    return (
      <section className="panel">
        <div className="space-y-3 p-3">
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span className="font-semibold uppercase tracking-wide">Pipeline</span>
            <span className="flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${live ? 'animate-pulse bg-emerald-400' : 'bg-slate-600'}`} />
              {live ? 'live' : aiIterations.length ? `${aiIterations.length} AI-iteratie${aiIterations.length > 1 ? 's' : ''}` : 'idle'}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded bg-slate-800">
            <div className={`h-full transition-all duration-500 ${bar}`} style={{ width: `${Math.round(job.progress * 100)}%` }} />
          </div>
          <ol className="space-y-0">
            {STEPS.map((s, i) => (
              <StepRow key={s.stage} label={i === STEPS.length - 1 && job.status === 'failed' ? 'Mislukt' : s.label}
                state={states[i]} last={i === STEPS.length - 1} />
            ))}
          </ol>
          {job.message && <p className="text-sm text-slate-300">{job.message}</p>}
          <details className="group">
            <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between rounded-md border border-slate-800 px-3 text-sm text-slate-300">
              Logboek ({events.length})
              <span className="text-slate-500 transition group-open:rotate-180">▾</span>
            </summary>
            <div className="mt-2">{log}</div>
          </details>
        </div>
      </section>
    )
  }

  return (
    <section className="panel">
      <div className="panel-title">
        <span>Pipeline</span>
        <span className="flex items-center gap-3 normal-case tracking-normal">
          {aiIterations.length > 0 && (
            <span className="text-[11px] font-normal text-slate-500">
              {aiIterations.length} iteratie{aiIterations.length > 1 ? 's' : ''} · max 1 + 3 herstelpogingen
            </span>
          )}
          <span className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full ${live ? 'animate-pulse bg-emerald-400' : 'bg-slate-600'}`} />
            <span className="text-[11px] font-normal text-slate-500">{live ? 'live' : 'idle'}</span>
          </span>
        </span>
      </div>
      <div className="space-y-2 p-3">
        <ol className="flex flex-wrap gap-1.5">
          {STEPS.map((s, i) => {
            const state = states[i]
            const cls = {
              active: 'border-sky-500 bg-sky-500/15 text-sky-200',
              complete: 'border-emerald-700 bg-emerald-500/10 text-emerald-300',
              done: 'border-emerald-500 bg-emerald-500/20 text-emerald-200',
              failed: 'border-rose-600 bg-rose-500/15 text-rose-200',
              skipped: 'border-slate-800 text-slate-600 line-through',
              pending: 'border-slate-800 text-slate-500',
            }[state]
            const label = i === STEPS.length - 1 && job.status === 'failed' ? 'Mislukt' : s.label
            return (
              <li key={s.stage} className={`rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${cls}`}>
                {state === 'active' && <span className="mr-1 inline-block animate-spin">◌</span>}
                {label}
              </li>
            )
          })}
        </ol>
        <div className="h-1.5 overflow-hidden rounded bg-slate-800">
          <div className={`h-full transition-all duration-500 ${bar}`} style={{ width: `${Math.round(job.progress * 100)}%` }} />
        </div>
        {job.message && <p className="truncate text-xs text-slate-400" title={job.message}>{job.message}</p>}
        {log}
      </div>
    </section>
  )
}

function StepRow({ label, state, last }: { label: string; state: StepState; last: boolean }) {
  const dot = {
    active: 'border-sky-400 bg-sky-500/20 text-sky-200',
    complete: 'border-emerald-500 bg-emerald-500 text-slate-950',
    done: 'border-emerald-400 bg-emerald-400 text-slate-950',
    failed: 'border-rose-500 bg-rose-500 text-white',
    skipped: 'border-slate-700 bg-transparent text-slate-600',
    pending: 'border-slate-700 bg-transparent text-slate-600',
  }[state]
  const text = {
    active: 'text-sky-200 font-medium',
    complete: 'text-slate-300',
    done: 'text-emerald-200 font-semibold',
    failed: 'text-rose-200 font-semibold',
    skipped: 'text-slate-600',
    pending: 'text-slate-500',
  }[state]
  const icon = state === 'complete' || state === 'done' ? '✓' : state === 'failed' ? '✕' : state === 'skipped' ? '–' : ''
  return (
    <li className="relative flex min-h-9 items-center gap-3">
      {!last && <span className="absolute top-7 left-[11px] h-[calc(100%-18px)] w-px bg-slate-800" aria-hidden />}
      <span className={`relative z-10 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 text-[11px] font-bold ${dot}`}>
        {state === 'active' ? <span className="h-2 w-2 animate-ping rounded-full bg-sky-400" /> : icon}
      </span>
      <span className={`text-sm ${text}`}>{label}</span>
      {state === 'skipped' && <span className="text-[11px] text-slate-600">niet nodig</span>}
    </li>
  )
}
