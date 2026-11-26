import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, validationLines } from '../api/client'
import type { DFMReport, EdgeTreatment, Feature, FeatureType, JobDetail, PartSpec, XY } from '../types/partSpec'
import { DATUMS, EDGE_TARGETS, FACES, REVIEW_CONFIDENCE_THRESHOLD, needsReview } from '../types/partSpec'

// ---- field model ------------------------------------------------------------------

type Kind = 'num' | 'numOpt' | 'enum' | 'pair' | 'points' | 'text' | 'bool' | 'holeKind'
interface FieldDef {
  key: string
  key2?: string // second key for 'pair'
  label: string
  kind: Kind
  options?: readonly string[]
  placeholder?: string
  wide?: boolean // spans two grid columns
  show?: (f: Rec) => boolean
  disabled?: (f: Rec) => boolean
}
type Rec = Record<string, unknown>

const FACE: FieldDef = { key: 'face', label: 'vlak', kind: 'enum', options: FACES }
const CENTER: FieldDef = { key: 'center_x', key2: 'center_y', label: 'midden x, y', kind: 'pair' }
const DEPTH: FieldDef = { key: 'depth', label: 'diepte', kind: 'numOpt', placeholder: 'door', disabled: (f) => !!f.through_all }
const THRU: FieldDef = { key: 'through_all', label: 'door', kind: 'bool' }
const hasPattern = (t: string) => (f: Rec) => (f.pattern as Rec | null | undefined)?.type === t

const PATTERN: FieldDef[] = [
  { key: 'pattern.count_x', label: 'aantal x', kind: 'num', show: hasPattern('linear') },
  { key: 'pattern.count_y', label: 'aantal y', kind: 'num', show: hasPattern('linear') },
  { key: 'pattern.pitch_x', label: 'steek x', kind: 'num', show: hasPattern('linear') },
  { key: 'pattern.pitch_y', label: 'steek y', kind: 'num', show: hasPattern('linear') },
  { key: 'pattern.count', label: 'aantal rond', kind: 'num', show: hasPattern('circular') },
  { key: 'pattern.radius', label: 'R steekcirkel', kind: 'num', show: hasPattern('circular') },
  { key: 'pattern.start_angle_deg', label: 'start °', kind: 'num', show: hasPattern('circular') },
]

const FIELDS: Record<FeatureType, FieldDef[]> = {
  hole: [
    FACE, CENTER, { key: 'diameter', label: 'Ø', kind: 'num' }, DEPTH, THRU,
    { key: 'hole_kind', label: 'soort', kind: 'holeKind' },
    { key: 'counterbore.diameter', label: 'verzink Ø', kind: 'num', show: (f) => !!f.counterbore },
    { key: 'counterbore.depth', label: 'verzinkdiepte', kind: 'num', show: (f) => !!f.counterbore },
    { key: 'countersink.diameter', label: 'csk Ø', kind: 'num', show: (f) => !!f.countersink },
    { key: 'countersink.angle_deg', label: 'csk °', kind: 'num', show: (f) => !!f.countersink },
    { key: 'thread', label: 'draad', kind: 'text', placeholder: 'geen (bv. M8)' },
    { key: 'thread_depth', label: 'draaddiepte', kind: 'numOpt', show: (f) => !!f.thread },
    { key: 'tolerance', label: 'tolerantie', kind: 'text', placeholder: 'geen (bv. H7)' },
    ...PATTERN,
  ],
  pocket_rect: [
    FACE, CENTER, { key: 'length_x', label: 'lengte x', kind: 'num' }, { key: 'width_y', label: 'breedte y', kind: 'num' },
    { key: 'corner_radius', label: 'hoek R', kind: 'num' }, { key: 'rotation_deg', label: 'rotatie °', kind: 'num' },
    DEPTH, THRU, ...PATTERN,
  ],
  pocket_circle: [FACE, CENTER, { key: 'diameter', label: 'Ø', kind: 'num' }, DEPTH, THRU, ...PATTERN],
  pocket_polygon: [
    FACE, { key: 'points_2d', label: 'punten x, y; …', kind: 'points', wide: true },
    { key: 'corner_radius', label: 'hoek R', kind: 'num' }, DEPTH, THRU,
  ],
  slot: [
    FACE, { key: 'start_x', key2: 'start_y', label: 'start x, y', kind: 'pair' },
    { key: 'end_x', key2: 'end_y', label: 'eind x, y', kind: 'pair' },
    { key: 'width', label: 'breedte', kind: 'num' }, DEPTH, THRU,
  ],
  boss_rect: [
    FACE, CENTER, { key: 'length_x', label: 'lengte x', kind: 'num' }, { key: 'width_y', label: 'breedte y', kind: 'num' },
    { key: 'height', label: 'hoogte', kind: 'num' }, { key: 'corner_radius', label: 'hoek R', kind: 'num' },
    { key: 'rotation_deg', label: 'rotatie °', kind: 'num' }, ...PATTERN,
  ],
  boss_circle: [FACE, CENTER, { key: 'diameter', label: 'Ø', kind: 'num' }, { key: 'height', label: 'hoogte', kind: 'num' }, ...PATTERN],
  step_cut: [
    FACE, { key: 'x_min', key2: 'x_max', label: 'x van, tot', kind: 'pair' },
    { key: 'y_min', key2: 'y_max', label: 'y van, tot', kind: 'pair' },
    { key: 'depth', label: 'diepte', kind: 'num' }, { key: 'corner_radius', label: 'hoek R', kind: 'num' },
  ],
  complex_3d: [
    { key: 'operation', label: 'bewerking', kind: 'enum', options: ['cut', 'union'] },
    { key: 'description', label: 'omschrijving', kind: 'text', wide: true },
  ],
}

const TEMPLATES: { [K in FeatureType]: Omit<Extract<Feature, { type: K }>, 'id'> } = {
  hole: { type: 'hole', face: 'top', center_x: 10, center_y: 10, diameter: 6.6, depth: null, through_all: true, confidence: 1 },
  pocket_rect: {
    type: 'pocket_rect', face: 'top', center_x: 30, center_y: 20, length_x: 20, width_y: 10, corner_radius: 3,
    rotation_deg: 0, depth: 5, through_all: false, confidence: 1,
  },
  pocket_circle: { type: 'pocket_circle', face: 'top', center_x: 30, center_y: 20, diameter: 20, depth: 5, through_all: false, confidence: 1 },
  pocket_polygon: {
    type: 'pocket_polygon', face: 'top', points_2d: [[10, 10], [30, 10], [20, 25]], corner_radius: 2, depth: 3,
    through_all: false, confidence: 1,
  },
  slot: { type: 'slot', face: 'top', start_x: 10, start_y: 10, end_x: 30, end_y: 10, width: 6, depth: 3, through_all: false, confidence: 1 },
  boss_rect: {
    type: 'boss_rect', face: 'top', center_x: 30, center_y: 20, length_x: 20, width_y: 10, height: 5, corner_radius: 0,
    rotation_deg: 0, confidence: 1,
  },
  boss_circle: { type: 'boss_circle', face: 'top', center_x: 30, center_y: 20, diameter: 10, height: 5, confidence: 1 },
  step_cut: { type: 'step_cut', face: 'top', x_min: 0, x_max: 10, y_min: 0, y_max: 50, depth: 5, corner_radius: 0, confidence: 1 },
  complex_3d: { type: 'complex_3d', face: 'top', description: 'beschrijf de niet-prismatische vorm', operation: 'cut', confidence: 1 },
}

const PREFIX: Record<FeatureType, string> = {
  hole: 'H', pocket_rect: 'P', pocket_circle: 'P', pocket_polygon: 'P', slot: 'S', boss_rect: 'B', boss_circle: 'B',
  step_cut: 'SC', complex_3d: 'X',
}

const TYPE_LABEL: Record<FeatureType, string> = {
  hole: 'gat', pocket_rect: 'kamer ▭', pocket_circle: 'kamer ○', pocket_polygon: 'kamer ⬠', slot: 'sleuf',
  boss_rect: 'nok ▭', boss_circle: 'nok ○', step_cut: 'stap', complex_3d: '3D (AI)',
}

const GRID = 'grid grid-cols-2 gap-x-3 gap-y-2.5 sm:grid-cols-3 md:grid-cols-4 2xl:grid-cols-6'

// ---- helpers ------------------------------------------------------------------------

function getPath(obj: Rec, path: string): unknown {
  return path.split('.').reduce<unknown>((o, k) => (o && typeof o === 'object' ? (o as Rec)[k] : undefined), obj)
}

function setPath<T extends object>(obj: T, path: string, value: unknown): T {
  const keys = path.split('.')
  const clone = structuredClone(obj) as Rec
  let cur = clone
  keys.slice(0, -1).forEach((k) => {
    if (!cur[k] || typeof cur[k] !== 'object') cur[k] = {}
    cur = cur[k] as Rec
  })
  cur[keys[keys.length - 1]] = value
  return clone as T
}

const fmtPair = (a: unknown, b: unknown) => (a == null || b == null ? '' : `${a}, ${b}`)
function parsePair(s: string): [number, number] | null {
  const m = s.split(/[;,\s]+/).filter(Boolean).map((x) => Number(x.replace(',', '.')))
  return m.length === 2 && m.every(Number.isFinite) ? [m[0], m[1]] : null
}
const fmtPoints = (ps?: XY[] | null) => (ps ?? []).map((p) => `${p[0]}, ${p[1]}`).join('; ')
function parsePoints(s: string): XY[] | null {
  const pts = s.split(';').map((x) => x.trim()).filter(Boolean).map(parsePair)
  return pts.length >= 3 && pts.every(Boolean) ? (pts as XY[]) : null
}

// ---- component --------------------------------------------------------------------------

interface Props {
  jobId: string
  spec: PartSpec
  version: number
  report: DFMReport | null
  selectedId: string | null
  onSelect: (id: string | null) => void
  busy: boolean
  onRebuilt: (detail: JobDetail) => void
  onReextract: () => void
}

export default function FeatureEditor({ jobId, spec, version, report, selectedId, onSelect, busy, onRebuilt, onReextract }: Props) {
  const [draft, setDraft] = useState<PartSpec>(spec)
  const [mode, setMode] = useState<'table' | 'json'>('table')
  const [jsonText, setJsonText] = useState('')
  const [jsonError, setJsonError] = useState<string | null>(null)
  const [errors, setErrors] = useState<string[]>([])
  const [submitting, setSubmitting] = useState<'patch' | 'reextract' | null>(null)
  const [lastBuild, setLastBuild] = useState<string | null>(null)
  const [hint, setHint] = useState('')
  const [showAi, setShowAi] = useState(false)
  const rowRefs = useRef<Record<string, HTMLDivElement | null>>({})

  useEffect(() => {
    setDraft(spec)
    setErrors([])
  }, [spec, version])

  useEffect(() => {
    if (selectedId) rowRefs.current[selectedId]?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [selectedId])

  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(spec), [draft, spec])
  const failed = useMemo(() => new Map(report?.feature_status.filter((s) => !s.ok).map((s) => [s.feature_id, s.error]) ?? []), [report])
  const flagged = useMemo(() => new Set(report?.issues.filter((i) => i.severity !== 'info' && i.feature_id).map((i) => i.feature_id!) ?? []), [report])
  const reviewCount =
    draft.features.filter((f) => needsReview(f.confidence)).length +
    draft.edge_treatments.filter((e) => needsReview(e.confidence)).length +
    (needsReview(draft.stock.confidence) ? 1 : 0) +
    (needsReview(draft.outer_profile.confidence) ? 1 : 0)

  const updFeature = (idx: number, path: string, value: unknown) =>
    setDraft((d) => ({ ...d, features: d.features.map((f, i) => (i === idx ? setPath(f, path, value) : f)) }))
  const updEdge = (idx: number, path: string, value: unknown) =>
    setDraft((d) => ({ ...d, edge_treatments: d.edge_treatments.map((e, i) => (i === idx ? setPath(e, path, value) : e)) }))

  function nextId(prefix: string) {
    const used = new Set([...draft.features.map((f) => f.id), ...draft.edge_treatments.map((e) => e.id)])
    let n = 1
    while (used.has(`${prefix}${n}`)) n++
    return `${prefix}${n}`
  }

  function addFeature(type: FeatureType) {
    const f = { ...structuredClone(TEMPLATES[type]), id: nextId(PREFIX[type]) } as Feature
    setDraft((d) => ({ ...d, features: [...d.features, f], requires_complex_3d_fallback: d.requires_complex_3d_fallback || type === 'complex_3d' }))
    onSelect(f.id)
  }

  function addEdge() {
    const e: EdgeTreatment = { id: nextId('E'), type: 'chamfer', size: 0.5, target: 'top_outer_edges', confidence: 1 }
    setDraft((d) => ({ ...d, edge_treatments: [...d.edge_treatments, e] }))
  }

  function switchMode(m: 'table' | 'json') {
    if (m === 'json') {
      setJsonText(JSON.stringify(draft, null, 2))
      setJsonError(null)
    }
    setMode(m)
  }

  async function rebuild() {
    setSubmitting('patch')
    setErrors([])
    try {
      const t0 = performance.now()
      const detail = await api.patchSpec(jobId, draft)
      setLastBuild(`v${detail.version} · ${((performance.now() - t0) / 1000).toFixed(1)} s`)
      onRebuilt(detail)
    } catch (e) {
      setErrors(validationLines(e))
    } finally {
      setSubmitting(null)
    }
  }

  async function reextract() {
    setSubmitting('reextract')
    setErrors([])
    try {
      await api.reextract(jobId, hint.trim())
      setHint('')
      setShowAi(false)
      onReextract()
    } catch (e) {
      setErrors(validationLines(e))
    } finally {
      setSubmitting(null)
    }
  }

  const cardCls = (id: string, conf: number) =>
    `scroll-mt-4 rounded-lg border p-3 transition-colors ${
      selectedId === id
        ? 'border-sky-500 bg-sky-500/5 ring-1 ring-sky-500/40'
        : failed.has(id)
          ? 'border-rose-700 bg-rose-500/5'
          : needsReview(conf)
            ? 'border-amber-500/80 bg-amber-500/10'
            : flagged.has(id)
              ? 'border-amber-900/80 bg-slate-950/40'
              : 'border-slate-800 bg-slate-950/40'
    }`

  return (
    <section className="panel flex h-full min-h-0 flex-col">
      <div className="panel-title">
        <span className="flex flex-wrap items-center gap-2">
          PartSpec · v{version}
          {dirty && <span className="rounded bg-sky-500/20 px-1.5 py-0.5 text-[10px] text-sky-200">gewijzigd</span>}
          {reviewCount > 0 && (
            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-300" title={`zekerheid < ${REVIEW_CONFIDENCE_THRESHOLD}`}>
              {reviewCount} te controleren
            </span>
          )}
        </span>
        <div className="flex gap-1 normal-case tracking-normal">
          <button className={`btn ${mode === 'table' ? 'btn-primary' : ''}`} onClick={() => switchMode('table')}>Formulier</button>
          <button className={`btn ${mode === 'json' ? 'btn-primary' : ''}`} onClick={() => switchMode('json')}>JSON</button>
        </div>
      </div>

      {mode === 'table' ? (
        <div className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain p-3">
          {/* part header */}
          <div className={GRID}>
            <TextField label="onderdeel" value={draft.part_name} wide onChange={(v) => setDraft((d) => ({ ...d, part_name: v || null }))} />
            <TextField label="materiaal" value={draft.material} onChange={(v) => setDraft((d) => ({ ...d, material: v || null }))} />
            <TextField label="alg. tolerantie" value={draft.general_tolerance} onChange={(v) => setDraft((d) => ({ ...d, general_tolerance: v || null }))} />
            <Field label="nulpunt (mm)" wide>
              <select className="sel-input w-full" value={draft.datum_origin} onChange={(e) => setDraft((d) => ({ ...d, datum_origin: e.target.value as PartSpec['datum_origin'] }))}>
                {DATUMS.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            </Field>
          </div>

          {/* stock */}
          <div className={cardCls('stock', draft.stock.confidence)}>
            <CardHeader title="Stock" conf={draft.stock.confidence} onVerify={() => setDraft((d) => ({ ...d, stock: { ...d.stock, confidence: 1 } }))} />
            <div className={GRID}>
              {(['length_x', 'width_y', 'thickness_z'] as const).map((k) => (
                <Field key={k} label={{ length_x: 'lengte x', width_y: 'breedte y', thickness_z: 'dikte z' }[k]}>
                  <NumberField value={draft.stock[k]} optional={false} onChange={(v) => setDraft((d) => ({ ...d, stock: { ...d.stock, [k]: v } }))} />
                </Field>
              ))}
            </div>
          </div>

          {/* outer profile */}
          <div className={cardCls('outer_profile', draft.outer_profile.confidence)}>
            <CardHeader title="Buitencontour" conf={draft.outer_profile.confidence}
              onVerify={() => setDraft((d) => ({ ...d, outer_profile: { ...d.outer_profile, confidence: 1 } }))} />
            <div className={GRID}>
              <Field label="vorm">
                <select className="sel-input w-full" value={draft.outer_profile.type}
                  onChange={(e) => setDraft((d) => ({ ...d, outer_profile: { ...d.outer_profile, type: e.target.value as PartSpec['outer_profile']['type'] } }))}>
                  <option value="rectangle">rechthoek</option>
                  <option value="rounded_rectangle">afgeronde rechthoek</option>
                  <option value="polygon">polygoon</option>
                </select>
              </Field>
              {draft.outer_profile.type !== 'rectangle' && (
                <Field label="hoek R">
                  <NumberField value={draft.outer_profile.corner_radius} optional={false}
                    onChange={(v) => setDraft((d) => ({ ...d, outer_profile: { ...d.outer_profile, corner_radius: v as number } }))} />
                </Field>
              )}
              {draft.outer_profile.type === 'polygon' && (
                <Field label="punten x, y; …" wide>
                  <LazyText className="txt-input w-full" value={fmtPoints(draft.outer_profile.points_2d)} parse={parsePoints}
                    onCommit={(v) => setDraft((d) => ({ ...d, outer_profile: { ...d.outer_profile, points_2d: v } }))} />
                </Field>
              )}
            </div>
          </div>

          {/* features */}
          {draft.features.map((f, idx) => (
            <div key={idx} ref={(el) => { rowRefs.current[f.id] = el }} onClick={() => onSelect(f.id)} className={cardCls(f.id, f.confidence)}>
              <div className="mb-2.5 flex items-center gap-2">
                <input className="txt-input w-36 font-semibold sm:w-44" aria-label="feature-id" value={f.id}
                  onChange={(e) => updFeature(idx, 'id', e.target.value)} />
                <span className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 text-[11px] text-slate-300" title={f.type}>{TYPE_LABEL[f.type]}</span>
                <span className="ml-auto flex items-center gap-1.5">
                  <Conf value={f.confidence} onVerify={() => updFeature(idx, 'confidence', 1)} />
                  <button className="btn btn-danger !px-2" aria-label={`${f.id} verwijderen`}
                    onClick={(e) => { e.stopPropagation(); setDraft((d) => ({ ...d, features: d.features.filter((_, i) => i !== idx) })) }}>
                    <TrashIcon />
                  </button>
                </span>
              </div>
              <div className={GRID}>
                {FIELDS[f.type]
                  .filter((fd) => !fd.show || fd.show(f as unknown as Rec))
                  .map((fd) => <FieldInput key={fd.key} def={fd} rec={f as unknown as Rec} onChange={(path, v) => updFeature(idx, path, v)} />)}
              </div>
              {('extra_positions' in f && (f.extra_positions?.length ?? 0) > 0) || f.type === 'complex_3d' || f.source_note || failed.has(f.id) ? (
                <div className="mt-2 space-y-0.5 text-xs">
                  {'extra_positions' in f && (f.extra_positions?.length ?? 0) > 0 && (
                    <div className="text-violet-300">+{f.extra_positions!.length} extra posities (bewerk in JSON)</div>
                  )}
                  {f.type === 'complex_3d' && (
                    <div className={f.code ? 'text-emerald-300' : 'text-amber-300'}>{f.code ? 'fallbackcode aanwezig' : 'nog geen fallbackcode (gebruik AI-herextractie)'}</div>
                  )}
                  {f.source_note && <div className="text-slate-400">“{f.source_note}”</div>}
                  {failed.has(f.id) && <div className="text-rose-300">bouwfout: {failed.get(f.id)}</div>}
                </div>
              ) : null}
            </div>
          ))}
          {draft.features.length === 0 && <p className="text-sm text-slate-500">Geen features.</p>}

          {/* edge treatments */}
          {draft.edge_treatments.map((e, idx) => (
            <div key={`e${idx}`} className={cardCls(e.id, e.confidence)} onClick={() => onSelect(e.id)}>
              <div className="mb-2.5 flex items-center gap-2">
                <input className="txt-input w-36 font-semibold sm:w-44" aria-label="id" value={e.id} onChange={(ev) => updEdge(idx, 'id', ev.target.value)} />
                <span className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 text-[11px] text-slate-300">randbewerking</span>
                <span className="ml-auto flex items-center gap-1.5">
                  <Conf value={e.confidence} onVerify={() => updEdge(idx, 'confidence', 1)} />
                  <button className="btn btn-danger !px-2" aria-label={`${e.id} verwijderen`}
                    onClick={(ev) => { ev.stopPropagation(); setDraft((d) => ({ ...d, edge_treatments: d.edge_treatments.filter((_, i) => i !== idx) })) }}>
                    <TrashIcon />
                  </button>
                </span>
              </div>
              <div className={GRID}>
                <Field label="soort">
                  <select className="sel-input w-full" value={e.type} onChange={(ev) => updEdge(idx, 'type', ev.target.value)}>
                    <option value="chamfer">afschuining</option>
                    <option value="fillet">afronding</option>
                  </select>
                </Field>
                <Field label="maat"><NumberField value={e.size} optional={false} onChange={(v) => updEdge(idx, 'size', v)} /></Field>
                <Field label="randen" wide>
                  <select className="sel-input w-full" value={e.target} onChange={(ev) => updEdge(idx, 'target', ev.target.value)}>
                    {EDGE_TARGETS.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                </Field>
              </div>
            </div>
          ))}

          <div>
            <div className="field-label">toevoegen</div>
            <div className="flex flex-wrap gap-1.5">
              {(Object.keys(TEMPLATES) as FeatureType[]).map((t) => (
                <button key={t} className="btn" onClick={() => addFeature(t)}>+ {TYPE_LABEL[t]}</button>
              ))}
              <button className="btn" onClick={addEdge}>+ randbewerking</button>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col p-3">
          <textarea
            className="scroll-thin min-h-[320px] flex-1 resize-none rounded-md border border-slate-700 bg-slate-950 p-2 font-mono text-[11px] text-slate-200 outline-none focus:border-sky-500 [@media(pointer:coarse)]:text-[13px]"
            spellCheck={false}
            value={jsonText}
            onChange={(e) => {
              setJsonText(e.target.value)
              try {
                setDraft(JSON.parse(e.target.value) as PartSpec)
                setJsonError(null)
              } catch (err) {
                setJsonError(String(err))
              }
            }}
          />
          {jsonError && <p className="mt-1 text-xs text-rose-400">{jsonError}</p>}
        </div>
      )}

      {/* action bar: always visible at the bottom of the editor */}
      <div className="space-y-2 border-t border-slate-800 bg-slate-900/95 px-3 py-2.5 backdrop-blur">
        {errors.length > 0 && (
          <ul className="max-h-28 list-disc space-y-0.5 overflow-y-auto pl-5 text-xs text-rose-300">
            {errors.slice(0, 12).map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        )}
        <div className="flex items-center gap-2">
          <button className="btn btn-primary min-h-11 flex-1 !text-sm font-semibold sm:flex-none sm:px-5" disabled={busy || !!submitting || !!jsonError}
            onClick={() => void rebuild()} title="Deterministische rebuild zonder AI-aanroep (PATCH /spec)">
            {submitting === 'patch' ? 'Herberekenen…' : busy ? 'Pipeline loopt…' : 'Herbereken STEP'}
          </button>
          {dirty && (
            <button className="btn min-h-11" disabled={!!submitting} onClick={() => setDraft(spec)} title="Wijzigingen terugdraaien">
              <UndoIcon /> <span className="hidden sm:inline">Terugdraaien</span>
            </button>
          )}
          <button className={`btn min-h-11 ${showAi ? 'border-violet-600 text-violet-200' : ''}`} onClick={() => setShowAi((v) => !v)}
            aria-expanded={showAi} aria-label="AI opnieuw laten extraheren" title="AI opnieuw laten extraheren">
            <SparkIcon /> <span className="hidden sm:inline">AI</span>
          </button>
        </div>
        {lastBuild && !dirty && !submitting && <p className="text-xs text-emerald-300">✓ STEP herberekend: {lastBuild}</p>}
        {showAi && (
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              className="txt-input min-h-11 flex-1 !font-sans"
              placeholder="Hint, bv. 'Diepte van de pocket is 12mm' (optioneel)"
              value={hint}
              onChange={(e) => setHint(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && !busy && void reextract()}
            />
            <button className="btn min-h-11 border-violet-700 bg-violet-900/40 text-violet-100" disabled={busy || !!submitting} onClick={() => void reextract()}>
              {submitting === 'reextract' ? 'Starten…' : 'Opnieuw extraheren (AI)'}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

// ---- building blocks ---------------------------------------------------------------------------

function Field({ label, wide, children }: { label: string; wide?: boolean; children: ReactNode }) {
  return (
    <label className={`block min-w-0 ${wide ? 'col-span-2' : ''}`}>
      <span className="field-label">{label}</span>
      {children}
    </label>
  )
}

function CardHeader({ title, conf, onVerify }: { title: string; conf: number; onVerify: () => void }) {
  return (
    <div className="mb-2.5 flex items-center gap-2">
      <span className="text-sm font-semibold text-slate-100">{title}</span>
      <span className="ml-auto"><Conf value={conf} onVerify={onVerify} /></span>
    </div>
  )
}

function Conf({ value, onVerify }: { value: number; onVerify: () => void }) {
  const low = needsReview(value)
  return (
    <span className="flex items-center gap-1.5">
      <span title="Zekerheid van de AI-extractie"
        className={`rounded px-1.5 py-0.5 font-mono text-[11px] ${low ? 'bg-amber-500/25 text-amber-200' : 'bg-emerald-500/10 text-emerald-300'}`}>
        {Math.round(value * 100)}%
      </span>
      {low && (
        <button className="btn border-emerald-800 !px-2 text-emerald-300" title="Gecontroleerd tegen de tekening (zekerheid = 100%)"
          aria-label="Markeer als gecontroleerd" onClick={(e) => { e.stopPropagation(); onVerify() }}>
          ✓ <span className="hidden sm:inline">ok</span>
        </button>
      )}
    </span>
  )
}

function TextField({ label, value, wide, onChange }: { label: string; value?: string | null; wide?: boolean; onChange: (v: string) => void }) {
  return (
    <Field label={label} wide={wide}>
      <input className="txt-input w-full" value={value ?? ''} onChange={(e) => onChange(e.target.value)} />
    </Field>
  )
}

function FieldInput({ def, rec, onChange }: { def: FieldDef; rec: Rec; onChange: (path: string, v: unknown) => void }) {
  const value = getPath(rec, def.key)
  switch (def.kind) {
    case 'enum':
      return (
        <Field label={def.label} wide={def.wide}>
          <select className="sel-input w-full" value={String(value ?? '')} onChange={(e) => onChange(def.key, e.target.value)}>
            {def.options!.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </Field>
      )
    case 'num':
    case 'numOpt':
      return (
        <Field label={def.label} wide={def.wide}>
          <NumberField value={value as number | null | undefined} optional={def.kind === 'numOpt'} placeholder={def.placeholder}
            disabled={def.disabled?.(rec)} onChange={(v) => onChange(def.key, v)} />
        </Field>
      )
    case 'bool':
      return (
        <label className="flex min-w-0 cursor-pointer items-end gap-2 pb-2">
          <input type="checkbox" className="accent-sky-500" checked={!!value} onChange={(e) => onChange(def.key, e.target.checked)} />
          <span className="text-xs font-medium uppercase tracking-wide text-slate-300">{def.label}</span>
        </label>
      )
    case 'pair':
      return (
        <Field label={def.label} wide={def.wide}>
          <LazyText className="txt-input w-full text-right" value={fmtPair(value, getPath(rec, def.key2!))} parse={parsePair}
            onCommit={([a, b]) => { onChange(def.key, a); onChange(def.key2!, b) }} />
        </Field>
      )
    case 'points':
      return (
        <Field label={def.label} wide>
          <LazyText className="txt-input w-full" value={fmtPoints(value as XY[])} parse={parsePoints} onCommit={(v) => onChange(def.key, v)} />
        </Field>
      )
    case 'text':
      return (
        <Field label={def.label} wide={def.wide}>
          <input className="txt-input w-full" value={String(value ?? '')} placeholder={def.placeholder}
            onChange={(e) => onChange(def.key, e.target.value || null)} />
        </Field>
      )
    case 'holeKind': {
      const kind = rec.counterbore ? 'counterbore' : rec.countersink ? 'countersink' : 'plain'
      const d = Number(rec.diameter) || 6
      return (
        <Field label={def.label} wide>
          <select className="sel-input w-full" value={kind} onChange={(e) => {
            const k = e.target.value
            onChange('counterbore', k === 'counterbore' ? { diameter: +(d * 1.7).toFixed(1), depth: +(d * 0.9).toFixed(1) } : null)
            onChange('countersink', k === 'countersink' ? { diameter: +(d * 2).toFixed(1), angle_deg: 90 } : null)
          }}>
            <option value="plain">recht</option>
            <option value="counterbore">verzonken (cilindrisch)</option>
            <option value="countersink">verzonken (conisch)</option>
          </select>
        </Field>
      )
    }
  }
}

function NumberField({ value, optional, placeholder, disabled, onChange }: {
  value: number | null | undefined; optional: boolean; placeholder?: string; disabled?: boolean; onChange: (v: number | null) => void
}) {
  const [text, setText] = useState(value == null ? '' : String(value))
  useEffect(() => setText(value == null ? '' : String(value)), [value])
  const invalid = text !== '' && !Number.isFinite(Number(text.replace(',', '.')))
  return (
    <input
      className={`num-input ${invalid ? '!border-rose-500' : ''} ${disabled ? 'opacity-40' : ''}`}
      inputMode="decimal"
      enterKeyHint="next"
      disabled={disabled}
      value={text}
      placeholder={placeholder ?? (optional ? '—' : '0')}
      onChange={(e) => {
        setText(e.target.value)
        const t = e.target.value.trim().replace(',', '.')
        if (t === '' && optional) onChange(null)
        else if (t !== '' && Number.isFinite(Number(t))) onChange(Number(t))
      }}
    />
  )
}

function LazyText<T>({ value, parse, onCommit, className }: { value: string; parse: (s: string) => T | null; onCommit: (v: T) => void; className: string }) {
  const [text, setText] = useState(value)
  useEffect(() => setText(value), [value])
  return (
    <input
      className={`${className} ${parse(text) === null ? '!border-rose-500' : ''}`}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => {
        const p = parse(text)
        if (p !== null) onCommit(p)
        else setText(value)
      }}
      onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
    />
  )
}

// ---- icons ------------------------------------------------------------------------------------

const ico = { width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
function TrashIcon() {
  return <svg {...ico} aria-hidden><path d="M3 6h18M8 6V4h8v2m-9 0 1 14h8l1-14" /></svg>
}
function UndoIcon() {
  return <svg {...ico} aria-hidden><path d="M9 14 4 9l5-5" /><path d="M4 9h10a6 6 0 0 1 0 12h-3" /></svg>
}
function SparkIcon() {
  return <svg {...ico} aria-hidden><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6" /></svg>
}
