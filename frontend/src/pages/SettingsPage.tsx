import { useEffect, useState } from 'react'
import { api, validationLines, type ShopRates, type ViseSpec } from '../api/client'
import { PageHeader } from '../components/BatchDashboard'
import { navigate } from '../router'

type Tab = 'vises' | 'rates'

/** v2.0 settings: workshop vises (softjaw blanks) and shop rates (quoting). The tool library has its own page. */
export default function SettingsPage({ tab }: { tab: Tab }) {
  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Instellingen" />
      <div className="flex gap-1.5 border-b border-slate-800 px-3 py-2" role="tablist">
        {([['vises', 'Bankschroeven'], ['rates', 'Tarieven']] as const).map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={`btn ${tab === k ? 'btn-primary' : ''}`}
            onClick={() => navigate(`/settings/${k}`)}>{label}</button>
        ))}
        <button className="btn" onClick={() => navigate('/tools')}>Gereedschap</button>
      </div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-3">{tab === 'vises' ? <Vises /> : <Rates />}</div>
    </div>
  )
}

const VISE_FIELDS: [keyof ViseSpec, string][] = [
  ['jaw_width_mm', 'bekbreedte'], ['jaw_height_mm', 'bekhoogte'], ['jaw_depth_mm', 'bekdiepte'],
  ['max_opening_mm', 'max opening'], ['bolt_spacing_mm', 'boutafstand'], ['bolt_height_from_bottom_mm', 'bout hoogte'],
  ['bolt_clearance_dia_mm', 'boutgat Ø'], ['bolt_cbore_dia_mm', 'verzink Ø'], ['bolt_cbore_depth_mm', 'verzinkdiepte'],
  ['default_jaw_gap_mm', 'std opening'],
]

function Vises() {
  const [vises, setVises] = useState<ViseSpec[] | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const load = () => api.getVises().then((l) => setVises(l.vises)).catch((e: Error) => setMsg({ ok: false, text: e.message }))
  useEffect(() => void load(), [])
  if (!vises) return <p className="text-sm text-slate-500">Laden…</p>
  const upd = (i: number, k: keyof ViseSpec, v: string | number) => setVises(vises.map((x, j) => (j === i ? { ...x, [k]: v } : x)))
  const save = async () => {
    try {
      setVises((await api.putVises({ schema_version: '1.0', vises })).vises)
      setMsg({ ok: true, text: 'Bankschroeven opgeslagen' })
    } catch (e) {
      setMsg({ ok: false, text: validationLines(e).join(' · ') })
    }
  }
  return (
    <div className="space-y-3">
      {vises.map((v, i) => (
        <section key={i} className="panel p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <input className="txt-input w-40 font-semibold" value={v.id} aria-label="id" onChange={(e) => upd(i, 'id', e.target.value)} />
            <input className="txt-input min-w-0 flex-1" value={v.name} aria-label="naam" onChange={(e) => upd(i, 'name', e.target.value)} />
            <button className="btn btn-danger" onClick={() => setVises(vises.filter((_, j) => j !== i))}>Verwijderen</button>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            {VISE_FIELDS.map(([k, label]) => (
              <label key={k} className="block">
                <span className="field-label">{label} (mm)</span>
                <input className="num-input" type="number" step={0.1} value={v[k] as number} onChange={(e) => upd(i, k, Number(e.target.value))} />
              </label>
            ))}
          </div>
        </section>
      ))}
      <div className="flex flex-wrap gap-2">
        <button className="btn" onClick={() => setVises([...vises, { ...vises[vises.length - 1], id: `vise_${vises.length + 1}`, name: '' }])}
          disabled={!vises.length}>+ Bankschroef</button>
        <button className="btn btn-primary" onClick={() => void save()}>Opslaan</button>
        {msg && <p className={`w-full text-sm ${msg.ok ? 'text-emerald-300' : 'text-rose-300'}`}>{msg.text}</p>}
      </div>
    </div>
  )
}

function Rates() {
  const [r, setR] = useState<ShopRates | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  useEffect(() => {
    api.getRates().then(setR).catch((e: Error) => setMsg({ ok: false, text: e.message }))
  }, [])
  if (!r) return <p className="text-sm text-slate-500">Laden…</p>
  const save = async () => {
    try {
      setR(await api.putRates(r))
      setMsg({ ok: true, text: 'Tarieven opgeslagen' })
    } catch (e) {
      setMsg({ ok: false, text: validationLines(e).join(' · ') })
    }
  }
  const num = (value: number, on: (v: number) => void, step = 0.1) => (
    <input className="num-input" type="number" step={step} value={value} onChange={(e) => on(Number(e.target.value))} />
  )
  return (
    <div className="space-y-3">
      <section className="panel p-3">
        <h3 className="field-label">Machines</h3>
        <div className="grid gap-3 sm:grid-cols-2">
          {(Object.keys(r.machines) as (keyof ShopRates['machines'])[]).map((k) => (
            <div key={k} className="grid grid-cols-3 gap-2">
              <span className="col-span-3 text-sm text-slate-200">{r.machines[k].label}</span>
              {([['hourly_rate', `${r.currency}/h`], ['tool_change_sec', 'wissel s'], ['load_unload_sec', 'in/uit s']] as const).map(([f, label]) => (
                <label key={f} className="block"><span className="field-label">{label}</span>
                  {num(r.machines[k][f], (v) => setR({ ...r, machines: { ...r.machines, [k]: { ...r.machines[k], [f]: v } } }))}
                </label>
              ))}
            </div>
          ))}
        </div>
      </section>
      <section className="panel p-3">
        <h3 className="field-label">Instellen (minuten) en algemeen</h3>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {(Object.keys(r.setup_minutes) as (keyof ShopRates['setup_minutes'])[]).map((k) => (
            <label key={k} className="block"><span className="field-label">{k.replace(/_/g, ' ')}</span>
              {num(r.setup_minutes[k], (v) => setR({ ...r, setup_minutes: { ...r.setup_minutes, [k]: v } }))}
            </label>
          ))}
          <label className="block"><span className="field-label">zaagverlies (factor)</span>
            {num(r.saw_waste_factor, (v) => setR({ ...r, saw_waste_factor: v }), 0.01)}</label>
          <label className="block"><span className="field-label">standaardmarge %</span>
            {num(r.margin_pct_default, (v) => setR({ ...r, margin_pct_default: v }), 1)}</label>
          <label className="block"><span className="field-label">fijnfrees-factor</span>
            {num(r.fine_finish_factor, (v) => setR({ ...r, fine_finish_factor: v }))}</label>
        </div>
      </section>
      <section className="panel overflow-x-auto p-3">
        <h3 className="field-label">Materialen</h3>
        <table className="w-full min-w-[760px] text-sm">
          <thead className="text-left text-[11px] uppercase tracking-wide text-slate-400">
            <tr><th className="py-1">groep</th><th>{r.currency}/kg</th><th>MRR cm³/min</th><th>nafrezen min/100 cm²</th><th>gat s</th><th>tap s</th><th>ruim s</th><th>η spaan</th></tr>
          </thead>
          <tbody>
            {Object.entries(r.materials).map(([g, m]) => (
              <tr key={g}>
                <td className="py-1 pr-2 text-slate-200">{g}</td>
                {(['price_per_kg', 'mrr_cm3_per_min', 'finish_min_per_100cm2', 'hole_sec_base', 'tap_sec', 'ream_sec', 'chip_load_efficiency'] as const).map((f) => (
                  <td key={f} className="pr-2">{num(m[f], (v) => setR({ ...r, materials: { ...r.materials, [g]: { ...m, [f]: v } } }))}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <div className="flex flex-wrap gap-2">
        <button className="btn btn-primary" onClick={() => void save()}>Opslaan</button>
        {msg && <p className={`w-full text-sm ${msg.ok ? 'text-emerald-300' : 'text-rose-300'}`}>{msg.text}</p>}
      </div>
    </div>
  )
}
