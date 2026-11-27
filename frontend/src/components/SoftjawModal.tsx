import { useEffect, useState } from 'react'
import { ApiError, api, type SoftjawReport, type SoftjawRequest, type ViseSpec } from '../api/client'
import Step3DViewer from './Step3DViewer'

const LAYERS = [
  { name: 'Fixed_Jaw', label: 'Vaste bek' },
  { name: 'Movable_Jaw', label: 'Beweeglijke bek' },
  { name: 'Workpiece', label: 'Werkstuk' },
]

interface Props {
  jobId: string
  version: number
  onClose: () => void
}

/** v2.0 §3.2: Op 2 softjaws in one click: vise, flip, grip depth -> checks + orbitable assembly + STEP. */
export default function SoftjawModal({ jobId, version, onClose }: Props) {
  const [vises, setVises] = useState<ViseSpec[]>([])
  const [req, setReq] = useState<SoftjawRequest>({
    vise_id: 'vise_125_std', op2_flip_axis: 'X', z_rotation_deg: 0, grip_depth_mm: 4, xy_clearance_mm: 0.05,
    dogbone_radius_mm: 3.2, jaw_gap_mm: null, jaw_material: 'aluminum_6082',
  })
  const [busy, setBusy] = useState(false)
  const [report, setReport] = useState<SoftjawReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [hidden, setHidden] = useState<string[]>([])
  const [tick, setTick] = useState(0)

  useEffect(() => {
    api.getVises().then((l) => {
      setVises(l.vises)
      if (l.vises.length && !l.vises.some((v) => v.id === req.vise_id)) setReq((r) => ({ ...r, vise_id: l.vises[0].id }))
    }).catch(() => undefined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const vise = vises.find((v) => v.id === req.vise_id)
  const set = <K extends keyof SoftjawRequest>(k: K, v: SoftjawRequest[K]) => setReq((r) => ({ ...r, [k]: v }))

  const generate = async () => {
    setBusy(true)
    setError(null)
    try {
      setReport(await api.softjaws(jobId, { ...req, version } as Partial<SoftjawRequest>))
      setTick((t) => t + 1)
    } catch (e) {
      if (e instanceof ApiError && e.status === 422 && typeof e.detail === 'object' && e.detail && 'checks' in e.detail) {
        setReport(e.detail as SoftjawReport)
      } else {
        setReport(null)
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setBusy(false)
    }
  }

  const glb = report?.passed && report.urls?.['softjaws_assembly.glb'] ? `${report.urls['softjaws_assembly.glb']}?t=${tick}` : undefined

  return (
    <div className="fixed inset-0 z-30 flex items-stretch justify-center bg-black/60 p-0 sm:items-center sm:p-4" role="dialog"
      aria-modal="true" aria-label="Zachte bekken genereren" onClick={onClose}>
      <div className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden bg-slate-900 sm:rounded-xl sm:border sm:border-slate-700"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-slate-800 px-4 py-2">
          <h2 className="text-sm font-semibold text-slate-100">Zachte bekken voor Op 2 · v{version}</h2>
          <button className="btn ml-auto" onClick={onClose} aria-label="Sluiten">✕</button>
        </div>
        <div className="scroll-thin grid min-h-0 flex-1 gap-3 overflow-y-auto p-3 lg:grid-cols-[320px_1fr]">
          <div className="space-y-2.5">
            <Labeled label="Bankschroef">
              <select className="sel-input w-full" value={req.vise_id} onChange={(e) => set('vise_id', e.target.value)}>
                {vises.map((v) => <option key={v.id} value={v.id}>{v.name || v.id}</option>)}
              </select>
            </Labeled>
            <div className="grid grid-cols-2 gap-2">
              <Labeled label="Omdraaien om">
                <select className="sel-input w-full" value={req.op2_flip_axis} onChange={(e) => set('op2_flip_axis', e.target.value as 'X' | 'Y')}>
                  <option value="X">X (standaard)</option>
                  <option value="Y">Y</option>
                </select>
              </Labeled>
              <Labeled label="Draaien om Z">
                <select className="sel-input w-full" value={req.z_rotation_deg} onChange={(e) => set('z_rotation_deg', Number(e.target.value) as 0 | 90)}>
                  <option value={0}>0°</option>
                  <option value={90}>90°</option>
                </select>
              </Labeled>
            </div>
            <Labeled label={`Klemdiepte ${req.grip_depth_mm.toFixed(1)} mm`}>
              <input type="range" min={2} max={25} step={0.5} value={req.grip_depth_mm} className="h-11 w-full accent-sky-500"
                onChange={(e) => set('grip_depth_mm', Number(e.target.value))} />
            </Labeled>
            <div className="grid grid-cols-2 gap-2">
              <Labeled label="Spelingsmaat (mm)">
                <input className="num-input" type="number" step={0.01} min={0} max={1} value={req.xy_clearance_mm}
                  onChange={(e) => set('xy_clearance_mm', Number(e.target.value))} />
              </Labeled>
              <Labeled label="Dogbone R (mm)">
                <input className="num-input" type="number" step={0.1} min={0.5} max={12} value={req.dogbone_radius_mm}
                  onChange={(e) => set('dogbone_radius_mm', Number(e.target.value))} />
              </Labeled>
              <Labeled label={`Bekopening (std ${vise?.default_jaw_gap_mm ?? '–'})`}>
                <input className="num-input" type="number" step={0.5} min={0} placeholder={String(vise?.default_jaw_gap_mm ?? '')}
                  value={req.jaw_gap_mm ?? ''} onChange={(e) => set('jaw_gap_mm', e.target.value === '' ? null : Number(e.target.value))} />
              </Labeled>
              <Labeled label="Bekmateriaal">
                <select className="sel-input w-full" value={req.jaw_material}
                  onChange={(e) => set('jaw_material', e.target.value as SoftjawRequest['jaw_material'])}>
                  <option value="aluminum_6082">aluminium 6082</option>
                  <option value="steel_c45">staal C45</option>
                </select>
              </Labeled>
            </div>
            <button className="btn btn-primary w-full" disabled={busy || !vise} onClick={() => void generate()}>
              {busy ? 'Bekken berekenen…' : report ? 'Opnieuw genereren' : 'Genereer zachte bekken'}
            </button>
            {error && <p className="text-sm text-rose-300">{error}</p>}
            {report && (
              <ul className="space-y-1 text-xs" aria-label="Veiligheidscontroles">
                {report.checks.map((c) => (
                  <li key={c.code} className={`rounded px-2 py-1 ${c.passed ? 'bg-emerald-500/10 text-emerald-200' : c.blocking ? 'bg-rose-500/15 text-rose-200' : 'bg-amber-500/10 text-amber-200'}`}>
                    {c.passed ? '✓' : c.blocking ? '✕' : '!'} {c.message}
                  </li>
                ))}
              </ul>
            )}
            {report?.passed && (
              <div className="space-y-1 text-xs text-slate-300">
                <p>Dogbones: {report.dogbones.length} · holte {report.cavity_depth_y_mm.toFixed(2)} mm diep per bek</p>
                {report.recommended_tool && (
                  <p>Frees: {report.recommended_tool.tool_id} {report.recommended_tool.tool_name} (Ø{report.recommended_tool.diameter_mm})</p>
                )}
                <div className="flex flex-wrap gap-1.5 pt-1">
                  <a className="btn btn-primary" href={report.urls?.['softjaws.step']}>softjaws.step</a>
                  <a className="btn" href={report.urls?.['softjaw_report.json']} target="_blank" rel="noreferrer">rapport</a>
                </div>
              </div>
            )}
          </div>
          <div className="flex min-h-[340px] flex-col gap-2">
            <div className="min-h-[300px] flex-1">
              {glb ? (
                <Step3DViewer glbUrl={glb} keepMaterials hiddenLayers={hidden} hideOrtho />
              ) : (
                <div className="panel flex h-full items-center justify-center p-6 text-center text-sm text-slate-500">
                  {report && !report.passed ? 'Geblokkeerd door een veiligheidscontrole: pas de instellingen aan.' : 'De bekken verschijnen hier na het genereren.'}
                </div>
              )}
            </div>
            {glb && (
              <div className="flex flex-wrap gap-1.5" role="group" aria-label="Lagen">
                {LAYERS.map((l) => {
                  const on = !hidden.includes(l.name)
                  return (
                    <button key={l.name} aria-pressed={on} className={`btn ${on ? 'border-sky-600 text-sky-200' : 'text-slate-500'}`}
                      onClick={() => setHidden((h) => (on ? [...h, l.name] : h.filter((x) => x !== l.name)))}>
                      {l.label}
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function Labeled({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="field-label">{label}</span>
      {children}
    </label>
  )
}
