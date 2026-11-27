import { useEffect, useState } from 'react'
import { api, type ChangeCategory, type RevisionDiffReport } from '../api/client'
import { PageHeader } from '../components/BatchDashboard'
import Step3DViewer from '../components/Step3DViewer'

const LAYERS = [
  { name: 'Added', label: 'Toegevoegd', swatch: '#1EB45A' },
  { name: 'Removed', label: 'Verwijderd', swatch: '#E61E1E' },
  { name: 'Unchanged', label: 'Ongewijzigd', swatch: '#B9BEC4' },
]
export const CATEGORY: Record<ChangeCategory | 'IDENTICAL', { label: string; cls: string }> = {
  BREAKING_CAM: { label: 'CAM opnieuw', cls: 'bg-rose-500/15 text-rose-200 border-rose-700' },
  GEOMETRY_CHANGE: { label: 'Geometrie', cls: 'bg-amber-500/15 text-amber-200 border-amber-700' },
  METADATA_ONLY: { label: 'Alleen titelblok', cls: 'bg-slate-700/40 text-slate-300 border-slate-600' },
  IDENTICAL: { label: 'Identiek', cls: 'bg-emerald-500/15 text-emerald-200 border-emerald-700' },
}

/** v2.0 §3.3: Rev A vs Rev B side by side: green / red / gray B-Rep layers + classified semantic changes. */
export default function RevisionDiffPage({ oldId, newId }: { oldId: string; newId: string }) {
  const [rep, setRep] = useState<RevisionDiffReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [hidden, setHidden] = useState<string[]>([])

  useEffect(() => {
    setRep(null)
    setError(null)
    api.revisionDiff(oldId, newId).then(setRep).catch((e: Error) => setError(e.message))
  }, [oldId, newId])

  const v = rep?.volumes
  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Revisievergelijking" subtitle={rep ? `${rep.part_number ?? ''} rev ${rep.revision_old ?? '?'} → ${rep.revision_new ?? '?'}` : undefined} />
      {error && <p className="p-4 text-sm text-rose-300">{error}</p>}
      {!rep && !error && <p className="p-4 text-sm text-slate-400">Booleaanse vergelijking berekenen…</p>}
      {rep && (
        <div className="grid min-h-0 flex-1 gap-3 overflow-y-auto p-3 lg:grid-cols-[1fr_420px] lg:overflow-hidden">
          <div className="flex min-h-[360px] flex-col gap-2">
            <div className="min-h-[320px] flex-1">
              <Step3DViewer glbUrl={rep.urls['revision_diff.glb']} keepMaterials hiddenLayers={hidden} hideOrtho />
            </div>
            <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Lagen">
              {LAYERS.map((l) => {
                const on = !hidden.includes(l.name)
                const vol = l.name === 'Added' ? v?.added_mm3 : l.name === 'Removed' ? v?.removed_mm3 : v?.unchanged_mm3
                return (
                  <button key={l.name} aria-pressed={on} className={`btn ${on ? '' : 'opacity-40'}`}
                    onClick={() => setHidden((h) => (on ? [...h, l.name] : h.filter((x) => x !== l.name)))}>
                    <span className="h-3.5 w-3.5 rounded-sm ring-1 ring-black/40" style={{ background: l.swatch }} />
                    {l.label}
                    {vol != null && <span className="font-mono text-[11px] text-slate-400">{vol.toFixed(1)} mm³</span>}
                  </button>
                )
              })}
              <a className="btn ml-auto" href={rep.urls['revision_diff_report.pdf']} target="_blank" rel="noreferrer">Rapport (PDF)</a>
            </div>
          </div>
          <section className="panel flex min-h-0 flex-col">
            <div className="panel-title">
              <span>Wijzigingen</span>
              <span className={`rounded border px-2 py-0.5 normal-case tracking-normal ${CATEGORY[rep.overall].cls}`}>{CATEGORY[rep.overall].label}</span>
            </div>
            <div className="flex flex-wrap gap-1.5 px-3 pt-2 text-[11px]">
              {Object.entries(rep.counts).filter(([, n]) => n > 0).map(([k, n]) => (
                <span key={k} className={`rounded border px-1.5 py-0.5 ${CATEGORY[k as ChangeCategory].cls}`}>{CATEGORY[k as ChangeCategory].label}: {n}</span>
              ))}
            </div>
            <ul className="scroll-thin min-h-0 flex-1 divide-y divide-slate-800 overflow-y-auto">
              {rep.items.map((it, i) => (
                <li key={i} className="px-3 py-2 text-sm">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className={`rounded border px-1.5 py-0.5 text-[11px] ${CATEGORY[it.category].cls}`}>{CATEGORY[it.category].label}</span>
                    <span className="text-[11px] uppercase tracking-wide text-slate-500">{it.change}</span>
                    {(it.id_new || it.id_old) && <span className="font-mono text-xs text-slate-300">{it.id_new ?? it.id_old}</span>}
                    {it.face && <span className="text-[11px] text-slate-500">{it.face}</span>}
                  </div>
                  <p className="mt-1 text-xs text-slate-300">{it.message}</p>
                </li>
              ))}
              {rep.items.length === 0 && <li className="p-3 text-sm text-slate-500">Geen wijzigingen in de PartSpec.</li>}
            </ul>
            <div className="flex flex-wrap gap-1.5 border-t border-slate-800 p-2">
              <a className="btn" href={`/#job=${rep.job_old_id}`}>Oude revisie</a>
              <a className="btn" href={`/#job=${rep.job_new_id}`}>Nieuwe revisie</a>
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
