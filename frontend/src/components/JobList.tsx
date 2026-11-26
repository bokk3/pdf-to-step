import type { JobSummary } from '../types/partSpec'
import { TERMINAL } from '../types/partSpec'
import { STATUS_LABEL } from './PipelineProgress'

export function statusDot(j: JobSummary): string {
  if (j.running || !TERMINAL.includes(j.status)) return 'bg-sky-400 animate-pulse'
  if (j.status === 'failed') return 'bg-rose-500'
  return j.dfm?.needs_review ? 'bg-amber-400' : 'bg-emerald-400'
}

interface Props {
  jobs: JobSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
  large?: boolean
}

export default function JobList({ jobs, selectedId, onSelect, large }: Props) {
  if (jobs.length === 0) return <p className="px-1 text-sm text-slate-500">Nog geen jobs.</p>
  return (
    <ul className="space-y-1.5">
      {jobs.map((j) => (
        <li key={j.job_id}>
          <button
            onClick={() => onSelect(j.job_id)}
            className={`w-full rounded-lg border text-left transition ${large ? 'px-3 py-3' : 'px-2 py-1.5'} ${
              j.job_id === selectedId ? 'border-sky-600 bg-sky-500/10' : 'border-slate-800/60 hover:bg-slate-900 active:bg-slate-900'
            }`}
          >
            <div className="flex items-center gap-2">
              <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${statusDot(j)}`} />
              <span className={`truncate text-slate-100 ${large ? 'text-base font-medium' : 'text-sm'}`}>{j.filename}</span>
              {large && <span className="ml-auto shrink-0 text-slate-500">›</span>}
            </div>
            <div className={`mt-0.5 flex flex-wrap gap-x-2 pl-[18px] text-slate-400 ${large ? 'text-xs' : 'text-[11px]'}`}>
              <span>{new Date(j.created_at * 1000).toLocaleString('nl-NL', { dateStyle: 'short', timeStyle: 'short' })}</span>
              {j.current_version > 0 && <span>v{j.current_version}</span>}
              <span>{STATUS_LABEL[j.status] ?? j.status}</span>
              {j.dfm && j.dfm.warnings > 0 && <span className="text-amber-400">{j.dfm.warnings} ⚠</span>}
            </div>
          </button>
        </li>
      ))}
    </ul>
  )
}
