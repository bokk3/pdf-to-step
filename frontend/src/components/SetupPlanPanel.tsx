import { useEffect, useState } from 'react'
import { api, type SetupPlan } from '../api/client'

const WORKHOLDING: Record<string, string> = {
  vise_hardjaws: 'harde bekken',
  vise_softjaws: 'zachte bekken',
  vise_5axis: '5-assige klem',
}
const FACE_NL: Record<string, string> = {
  top: 'boven', bottom: 'onder', front: 'voor', back: 'achter', left: 'links', right: 'rechts',
}
const faceLabel = (f: string) => FACE_NL[f] ?? f.replace('plane:', 'schuin ')

interface Props {
  jobId: string
  version: number
  tick?: number
  onSoftjaws?: () => void
}

/** v2.0 §3.1: 3-axis vs 5-axis 3+2 setup counts and the clamping sequence per machine type. */
export default function SetupPlanPanel({ jobId, version, tick = 0, onSoftjaws }: Props) {
  const [plan, setPlan] = useState<SetupPlan | null>(null)
  const [mode, setMode] = useState<'3' | '5'>('3')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setError(null)
    api.setupPlan(jobId).then(setPlan).catch((e: Error) => {
      setPlan(null)
      setError(e.message)
    })
  }, [jobId, version, tick])

  if (error) return null
  if (!plan) return <section className="panel p-3 text-sm text-slate-500">Opspanplan laden…</section>
  const steps = mode === '3' ? plan.setups_3axis : plan.setups_5axis_3plus2

  return (
    <section className="panel">
      <div className="panel-title">
        <span>Opspanplan</span>
        <div className="flex gap-1 normal-case tracking-normal" role="tablist" aria-label="Machinetype">
          <button role="tab" aria-selected={mode === '3'} className={`btn ${mode === '3' ? 'btn-primary' : ''}`} onClick={() => setMode('3')}>
            3-assig · {plan.min_setups_3axis}×
          </button>
          <button role="tab" aria-selected={mode === '5'} className={`btn ${mode === '5' ? 'btn-primary' : ''}`} onClick={() => setMode('5')}>
            5-assig 3+2 · {plan.min_setups_5axis_3plus2}×
          </button>
        </div>
      </div>
      <ol className="divide-y divide-slate-800">
        {steps.map((s) => (
          <li key={s.index} className="flex gap-3 px-3 py-2">
            <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-800 font-mono text-xs text-slate-200">
              Op{s.index}
            </span>
            <div className="min-w-0 flex-1 text-sm">
              <div className="flex flex-wrap items-center gap-1.5">
                {s.faces.map((f) => (
                  <span key={f} className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[11px] text-sky-200">{faceLabel(f)}</span>
                ))}
                <span className={`rounded px-1.5 py-0.5 text-[11px] ${s.workholding === 'vise_softjaws' ? 'bg-amber-500/15 text-amber-200' : 'bg-slate-800 text-slate-300'}`}>
                  {WORKHOLDING[s.workholding]}
                </span>
                {s.deck_off && <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[11px] text-slate-300">op dikte vlakken</span>}
              </div>
              <p className="mt-1 text-xs text-slate-400">{s.clamping}</p>
              {s.feature_ids.length > 0 && (
                <p className="mt-0.5 font-mono text-[11px] text-slate-500">{s.feature_ids.join(', ')}</p>
              )}
            </div>
          </li>
        ))}
      </ol>
      {(plan.softjaws_recommended || plan.notes.length > 0) && (
        <div className="space-y-1.5 border-t border-slate-800 px-3 py-2 text-xs">
          {plan.softjaws_recommended && (
            <div className="flex flex-wrap items-center gap-2 text-amber-200">
              Op 2 klemt niet meer op rechte flanken: zachte bekken aanbevolen.
              {onSoftjaws && <button className="btn" onClick={onSoftjaws}>Zachte bekken maken</button>}
            </div>
          )}
          {plan.notes.map((n) => <p key={n} className="text-slate-400">{n}</p>)}
        </div>
      )}
    </section>
  )
}
