import type { CamColorInfo } from '../api/client'

const ORDER = ['thread', 'tight_fit', 'seat', 'fine_finish', 'datum', 'body']
export const CAM_LABEL: Record<string, string> = {
  thread: 'Schroefdraad',
  tight_fit: 'Passing (IT ≤ 7)',
  seat: 'Verzinking / zitting',
  fine_finish: 'Fijn oppervlak',
  datum: 'Nulpunt-vlak (G54)',
  body: 'Overig',
}

interface Props {
  palette: Record<string, CamColorInfo>
  counts: Record<string, number>
  active: string[] | null // categories shown; null = all
  onChange: (next: string[] | null) => void
}

/** v1.5 §2.2: CAM face-colour legend; tap a colour to show only those faces (tap again for all). */
export default function CamColorLegend({ palette, counts, active, onChange }: Props) {
  const cats = ORDER.filter((k) => palette[k] && (k === 'body' || counts[k]))
  const toggle = (k: string) => {
    if (!active) return onChange([k])
    const next = active.includes(k) ? active.filter((x) => x !== k) : [...active, k]
    onChange(next.length === 0 || next.length === cats.length ? null : next)
  }
  return (
    <div className="flex flex-wrap gap-1.5" role="group" aria-label="CAM-kleurlegenda">
      {cats.map((k) => {
        const on = !active || active.includes(k)
        return (
          <button
            key={k}
            type="button"
            aria-pressed={on}
            onClick={() => toggle(k)}
            title={palette[k].meaning}
            className={`btn !justify-start gap-2 ${on ? '' : 'opacity-40'}`}
          >
            <span className="h-3.5 w-3.5 shrink-0 rounded-sm ring-1 ring-black/40" style={{ background: palette[k].hex }} />
            <span>{CAM_LABEL[k] ?? palette[k].name}</span>
            {k !== 'body' && <span className="font-mono text-[11px] text-slate-400">{counts[k] ?? 0}</span>}
          </button>
        )
      })}
      {active && (
        <button type="button" className="btn text-slate-400" onClick={() => onChange(null)}>
          Alles
        </button>
      )}
    </div>
  )
}
