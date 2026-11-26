import type { CriticReport, DFMReport, Severity } from '../types/partSpec'

const ORDER: Record<Severity, number> = { error: 0, warning: 1, info: 2 }
const STYLE: Record<Severity, { dot: string; text: string; label: string }> = {
  error: { dot: 'bg-rose-500', text: 'text-rose-300', label: 'fouten' },
  warning: { dot: 'bg-amber-400', text: 'text-amber-200', label: 'waarschuwingen' },
  info: { dot: 'bg-sky-400', text: 'text-sky-200', label: 'info' },
}

interface Props {
  report: DFMReport | null
  ambiguities: string[]
  critic: CriticReport | null
  selectedFeature: string | null
  onSelectFeature: (id: string) => void
}

export default function DfmWarningsPanel({ report, ambiguities, critic, selectedFeature, onSelectFeature }: Props) {
  const issues = report ? [...report.issues].sort((a, b) => ORDER[a.severity] - ORDER[b.severity]) : []
  const counts = { error: 0, warning: 0, info: 0 }
  issues.forEach((i) => counts[i.severity]++)
  const g = report?.geometry
  const size = g ? g.bbox_max_mm.map((v, i) => v - g.bbox_min_mm[i]) : null

  return (
    <section className="panel flex h-full min-h-0 flex-col">
      <div className="panel-title">
        <span>Controle &amp; DFM{report ? ` · v${report.spec_version}` : ''}</span>
        {report && (
          <span className={`rounded px-1.5 py-0.5 text-[10px] ${report.passed ? 'bg-emerald-500/15 text-emerald-300' : 'bg-rose-500/15 text-rose-300'}`}>
            {report.passed ? 'GEOMETRIE OK' : 'GEOMETRIE FOUT'}
          </span>
        )}
      </div>
      {!report && <p className="p-3 text-sm text-slate-500">Het rapport verschijnt na de eerste build.</p>}
      {g && (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 border-b border-slate-800 px-3 py-2 text-xs">
          <dt className="text-slate-500">Geldige B-Rep</dt>
          <dd className={g.is_valid ? 'text-emerald-300' : 'text-rose-300'}>{g.is_valid ? 'ja' : 'NEE'}</dd>
          <dt className="text-slate-500">Solids</dt>
          <dd className={g.solid_count === 1 ? 'text-slate-200' : 'text-rose-300'}>{g.solid_count}</dd>
          <dt className="text-slate-500">Omhullende (mm)</dt>
          <dd className="font-mono text-slate-200">{size?.map((v) => v.toFixed(2)).join(' × ')}</dd>
          <dt className="text-slate-500">vs. stock ±0,01</dt>
          <dd className={g.bbox_matches_spec ? 'text-emerald-300' : 'text-amber-300'}>
            {g.bbox_matches_spec ? 'klopt' : `Δ ${g.bbox_deviation_mm.toFixed(3)} mm`}
          </dd>
          <dt className="text-slate-500">Volume</dt>
          <dd className="font-mono text-slate-200">{(g.volume_mm3 / 1000).toFixed(2)} cm³</dd>
          {report?.stats.mass_g !== undefined && (
            <>
              <dt className="text-slate-500">Massa (schatting)</dt>
              <dd className="font-mono text-slate-200">{report.stats.mass_g.toFixed(1)} g</dd>
            </>
          )}
          {report && Object.keys(report.colored_faces).length > 0 && (
            <>
              <dt className="text-slate-500">CAM-kleuren</dt>
              <dd className="text-slate-300">
                <span className="text-blue-400">■</span> draad {report.colored_faces.thread ?? 0} ·{' '}
                <span className="text-red-400">■</span> nauwe tol. {report.colored_faces.tight_tolerance ?? 0}
              </dd>
            </>
          )}
        </dl>
      )}
      {report && (
        <div className="flex gap-3 px-3 pt-2 text-[11px]">
          {(Object.keys(counts) as Severity[]).map((s) => (
            <span key={s} className={`flex items-center gap-1 ${STYLE[s].text}`}>
              <span className={`h-2 w-2 rounded-full ${STYLE[s].dot}`} />
              {counts[s]} {STYLE[s].label}
            </span>
          ))}
        </div>
      )}
      <ul className="scroll-thin min-h-0 flex-1 space-y-1.5 overflow-y-auto p-3">
        {ambiguities.length > 0 && (
          <li className="rounded border border-amber-700/70 bg-amber-500/10 px-2 py-1.5 text-xs">
            <div className="mb-1 font-mono text-[10px] text-amber-300">ONDUIDELIJKHEDEN (AI)</div>
            <ul className="list-disc space-y-0.5 pl-4 text-amber-100">
              {ambiguities.map((a, n) => (
                <li key={n}>{a}</li>
              ))}
            </ul>
          </li>
        )}
        {critic && critic.verdict !== 'skipped' && (
          <li className={`rounded border px-2 py-1.5 text-xs ${critic.verdict === 'match' ? 'border-emerald-800 bg-emerald-500/5' : 'border-violet-700 bg-violet-500/10'}`}>
            <div className="font-mono text-[10px] text-violet-300">
              VISUELE CONTROLE: {critic.verdict === 'match' ? 'komt overeen met tekening' : 'afwijkingen'}
            </div>
            {critic.discrepancies.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-slate-300">
                {critic.discrepancies.map((d, n) => (
                  <li key={n}>
                    {d.feature_id && <b>{d.feature_id}: </b>}
                    {d.issue}
                  </li>
                ))}
              </ul>
            )}
          </li>
        )}
        {report && issues.length === 0 && <li className="text-sm text-emerald-300">Geen maakbaarheidsproblemen gevonden.</li>}
        {issues.map((i, n) => (
          <li
            key={n}
            className={`rounded border px-2 py-1.5 text-xs ${
              i.feature_id && i.feature_id === selectedFeature ? 'border-sky-500 bg-sky-500/10' : 'border-slate-800 bg-slate-950/40'
            }`}
          >
            <div className="flex items-start gap-2">
              <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${STYLE[i.severity].dot}`} />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className={`font-mono text-[10px] ${STYLE[i.severity].text}`}>{i.code}</span>
                  {i.feature_id && (
                    <button
                      className="rounded bg-slate-800 px-1 font-mono text-[10px] text-sky-300 hover:bg-slate-700"
                      onClick={() => onSelectFeature(i.feature_id!)}
                    >
                      {i.feature_id}
                    </button>
                  )}
                </div>
                <p className="text-slate-300">{i.message}</p>
                {i.suggestion && <p className="text-slate-500">→ {i.suggestion}</p>}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
