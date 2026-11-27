import { useEffect, useMemo, useRef, useState } from 'react'
import { api, validationLines, type ToolLibrary, type ToolSpec } from '../api/client'
import { PageHeader } from './BatchDashboard'

const TYPES = ['flat_endmill', 'bull_endmill', 'ball_endmill', 'slot_mill', 'face_mill', 'chamfer_mill', 'corner_rounding',
  'spot_drill', 'center_drill', 'drill', 'tap', 'thread_mill', 'reamer', 'boring_bar', 'counterbore', 'countersink', 'other']
const TYPE_LABEL: Record<string, string> = {
  flat_endmill: 'Vlakfrees', bull_endmill: 'Torusfrees', ball_endmill: 'Kogelfrees', slot_mill: 'Spiebaanfrees',
  face_mill: 'Planfrees', chamfer_mill: 'Afschuinfrees', corner_rounding: 'Afrondfrees', spot_drill: 'NC-centreerboor',
  center_drill: 'Centerboor', drill: 'Boor', tap: 'Tap', thread_mill: 'Draadfrees', reamer: 'Ruimer',
  boring_bar: 'Kotterbaar', counterbore: 'Verzinkboor (vlak)', countersink: 'Verzinkboor (kegel)', other: 'Overig',
}

/** v1.5 §2.1: the workshop tool library (data/config/tools.json) used by DFM v2 and the setup sheet. */
export default function ToolLibraryPage() {
  const [lib, setLib] = useState<ToolLibrary | null>(null)
  const [dirty, setDirty] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [filter, setFilter] = useState('')
  const [replace, setReplace] = useState(false)
  const file = useRef<HTMLInputElement>(null)

  const load = () => api.getTools().then((l) => { setLib(l); setDirty(false) }).catch((e: Error) => setMsg({ ok: false, text: e.message }))
  useEffect(() => void load(), [])

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase()
    return (lib?.tools ?? []).map((t, i) => [t, i] as const)
      .filter(([t]) => !q || `${t.id} ${t.name} ${t.tool_type} ${t.diameter_mm} ${t.thread_spec ?? ''}`.toLowerCase().includes(q))
  }, [lib, filter])

  const edit = (i: number, patch: Partial<ToolSpec>) => {
    if (!lib) return
    setLib({ ...lib, tools: lib.tools.map((t, j) => (j === i ? { ...t, ...patch } : t)) })
    setDirty(true)
  }
  const remove = (i: number) => {
    if (!lib) return
    setLib({ ...lib, tools: lib.tools.filter((_, j) => j !== i) })
    setDirty(true)
  }
  const add = () => {
    if (!lib) return
    const used = new Set(lib.tools.map((t) => t.id))
    let n = 1
    while (used.has(`T${String(n).padStart(2, '0')}`)) n++
    setLib({ ...lib, tools: [...lib.tools, { id: `T${String(n).padStart(2, '0')}`, name: '', tool_type: 'flat_endmill',
      diameter_mm: 6, flute_length_mm: 20, corner_radius_mm: 0, flutes: 3, materials: [], in_magazine: true }] })
    setDirty(true)
  }
  const save = async () => {
    if (!lib) return
    try {
      setLib(await api.putTools(lib))
      setDirty(false)
      setMsg({ ok: true, text: 'Gereedschapsbibliotheek opgeslagen' })
    } catch (e) {
      setMsg({ ok: false, text: validationLines(e).join(' · ') })
    }
  }
  const onImport = async (f: File | undefined) => {
    if (!f) return
    try {
      const r = await api.importTools(f, replace)
      setMsg({ ok: true, text: `${r.source}: ${r.added} nieuw, ${r.updated} bijgewerkt, ${r.skipped.length} overgeslagen (totaal ${r.total})` })
      void load()
    } catch (e) {
      setMsg({ ok: false, text: validationLines(e).join(' · ') })
    } finally {
      if (file.current) file.current.value = ''
    }
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Gereedschap" subtitle="Magazijn voor DFM-controle en instelblad" />
      <div className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <section className="panel flex flex-wrap items-center gap-2 p-3">
          <input className="txt-input w-48" placeholder="Zoeken (T04, Ø8, tap…)" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <button className="btn" onClick={add}>+ Gereedschap</button>
          <button className="btn btn-primary" disabled={!dirty} onClick={() => void save()}>Opslaan</button>
          {dirty && <button className="btn" onClick={() => void load()}>Wijzigingen weggooien</button>}
          <span className="mx-1 h-6 w-px bg-slate-800" />
          <input ref={file} type="file" accept=".json,.tools,.csv" className="hidden" onChange={(e) => void onImport(e.target.files?.[0])} />
          <button className="btn" onClick={() => file.current?.click()}>Fusion 360 / CSV importeren</button>
          <label className="flex items-center gap-1.5 text-xs text-slate-300">
            <input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> vervangen i.p.v. samenvoegen
          </label>
          {msg && <p className={`w-full text-sm ${msg.ok ? 'text-emerald-300' : 'text-rose-300'}`}>{msg.text}</p>}
        </section>

        <section className="panel overflow-x-auto">
          <table className="w-full min-w-[1040px] text-sm">
            <thead className="text-left text-[11px] uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-2 py-2">Nr.</th><th className="px-2 py-2">Naam</th><th className="px-2 py-2">Type</th>
                <th className="px-2 py-2">Ø mm</th><th className="px-2 py-2">Snijlengte</th><th className="px-2 py-2">Hoekradius</th>
                <th className="px-2 py-2">Draad / klasse</th><th className="px-2 py-2">In magazijn</th><th />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {shown.map(([t, i]) => (
                <tr key={i} className={t.in_magazine ? '' : 'opacity-50'}>
                  <td className="px-2 py-1"><input className="txt-input w-16" value={t.id} onChange={(e) => edit(i, { id: e.target.value.toUpperCase() })} /></td>
                  <td className="px-2 py-1"><input className="txt-input w-full min-w-[220px]" value={t.name} onChange={(e) => edit(i, { name: e.target.value })} /></td>
                  <td className="px-2 py-1">
                    <select className="sel-input" value={t.tool_type} onChange={(e) => edit(i, { tool_type: e.target.value })}>
                      {TYPES.map((k) => <option key={k} value={k}>{TYPE_LABEL[k]}</option>)}
                    </select>
                  </td>
                  <Num v={t.diameter_mm} on={(v) => edit(i, { diameter_mm: v })} />
                  <Num v={t.flute_length_mm} on={(v) => edit(i, { flute_length_mm: v })} />
                  <Num v={t.corner_radius_mm} on={(v) => edit(i, { corner_radius_mm: v })} />
                  <td className="px-2 py-1">
                    <input className="txt-input w-24" placeholder={t.tool_type === 'reamer' ? 'H7' : 'M8'}
                      value={(t.tool_type === 'reamer' ? t.tolerance_class : t.thread_spec) ?? ''}
                      disabled={!['tap', 'thread_mill', 'reamer'].includes(t.tool_type)}
                      onChange={(e) => edit(i, t.tool_type === 'reamer' ? { tolerance_class: e.target.value || null } : { thread_spec: e.target.value || null })} />
                  </td>
                  <td className="px-2 py-1 text-center">
                    <input type="checkbox" checked={t.in_magazine} onChange={(e) => edit(i, { in_magazine: e.target.checked })} aria-label={`${t.id} in magazijn`} />
                  </td>
                  <td className="px-2 py-1 text-right"><button className="btn btn-danger" onClick={() => remove(i)} aria-label={`${t.id} verwijderen`}>✕</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          {lib && shown.length === 0 && <p className="p-3 text-sm text-slate-500">Geen gereedschap gevonden.</p>}
        </section>
      </div>
    </div>
  )
}

function Num({ v, on }: { v: number; on: (v: number) => void }) {
  return (
    <td className="px-2 py-1">
      <input className="num-input w-20" type="number" step="0.01" min={0} value={v}
        onChange={(e) => on(e.target.value === '' ? 0 : Number(e.target.value))} />
    </td>
  )
}
