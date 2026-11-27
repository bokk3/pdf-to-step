import { useCallback, useEffect, useMemo, useState } from 'react'
import type * as THREE from 'three'
import { api, type Shopfloor, type ShopfloorFeature } from '../api/client'
import CamColorLegend, { CAM_LABEL } from './CamColorLegend'
import ClippingControls from './ClippingControls'
import Step3DViewer from './Step3DViewer'

interface Pick {
  point: [number, number, number]
  category: string | null
  feature: ShopfloorFeature | null
}

/** v1.5 §2.4: read-only shopfloor view (QR target) for phones / tablets at the machine. No editing, no AI. */
export default function ShopfloorView({ jobId }: { jobId: string }) {
  const [data, setData] = useState<Shopfloor | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [active, setActive] = useState<string[] | null>(null)
  const [clip, setClip] = useState<THREE.Plane | null>(null)
  const [pick, setPick] = useState<Pick | null>(null)

  useEffect(() => {
    api.shopfloor(jobId).then(setData).catch((e: Error) => setError(e.message))
  }, [jobId])

  const hexToCat = useMemo(() => {
    const m: Record<string, string> = {}
    for (const [k, c] of Object.entries(data?.palette ?? {})) m[c.hex.toUpperCase()] = k
    return m
  }, [data])
  const highlight = useMemo(() => (active && data ? active.map((k) => data.palette[k].hex) : null), [active, data])

  const onPick = useCallback(
    (p: THREE.Vector3, hex: string | null) => {
      if (!data) return
      const category = hex ? hexToCat[hex] ?? null : null
      const diag = Math.hypot(...data.bbox.max.map((v, i) => v - data.bbox.min[i]))
      const sameCat = data.features.filter((f) => f.category === category && f.anchors.length)
      const pool = category && category !== 'body' && category !== 'datum' && sameCat.length ? sameCat : data.features
      let best: ShopfloorFeature | null = null
      let bestD = Infinity
      for (const f of pool) {
        for (const a of f.anchors) {
          const d = Math.hypot(a[0] - p.x, a[1] - p.y, a[2] - p.z)
          if (d < bestD) {
            bestD = d
            best = f
          }
        }
      }
      const limit = pool === sameCat ? Infinity : diag * 0.12
      setPick({ point: [p.x, p.y, p.z], category, feature: bestD <= limit ? best : null })
    },
    [data, hexToCat],
  )

  if (error) return <Centered text={`Onderdeel niet gevonden (${error})`} />
  if (!data) return <Centered text="Laden…" />

  const art = data.artifacts
  const size = data.bbox.max.map((v, i) => v - data.bbox.min[i]) as [number, number, number]
  const stock = data.stock_recommendation

  return (
    <div className="flex h-full flex-col bg-slate-950">
      <header className="border-b border-slate-800 px-4 py-2">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-base font-semibold text-slate-100">{data.part_name}</h1>
          <span className="font-mono text-xs text-slate-400">v{data.version}</span>
          {data.verified_at ? (
            <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[11px] text-emerald-300">gecontroleerd</span>
          ) : (
            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[11px] text-amber-200">niet gecontroleerd</span>
          )}
        </div>
        <p className="mt-0.5 text-xs text-slate-400">
          {data.material} · {size.map((v) => v.toFixed(1)).join(' × ')} mm · {data.general_tolerance}
          {stock && <> · ruw: {stock.profile_label} ({stock.recommended_blank_mm.x} × {stock.recommended_blank_mm.y} × {stock.recommended_blank_mm.z})</>}
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {art['drawing.pdf'] && <a className="btn btn-primary" href={art['drawing.pdf']} target="_blank" rel="noreferrer">Tekening</a>}
          {art['setup_sheet.pdf'] && <a className="btn btn-primary" href={art['setup_sheet.pdf']} target="_blank" rel="noreferrer">Instelblad</a>}
          {art['model.step'] && <a className="btn" href={art['model.step']}>STEP</a>}
        </div>
      </header>

      <div className="relative min-h-[300px] flex-1">
        <div className="absolute inset-0 p-2">
          <Step3DViewer
            glbUrl={art['preview.glb']}
            bboxMax={size}
            floorZ={data.bbox.min[2]}
            clipPlane={clip}
            highlight={highlight}
            onPick={onPick}
            markers={pick?.feature?.anchors}
            hideOrtho
          />
        </div>
      </div>

      <div className="space-y-2 border-t border-slate-800 px-3 py-2">
        <CamColorLegend palette={data.palette} counts={data.color_counts} active={active} onChange={setActive} />
        <ClippingControls bbox={data.bbox} onChange={setClip} />
        <p className="text-[11px] text-slate-500">Tik op het model voor de details van een feature.</p>
      </div>

      {pick && <FeatureDrawer pick={pick} data={data} onClose={() => setPick(null)} />}
    </div>
  )
}

function FeatureDrawer({ pick, data, onClose }: { pick: Pick; data: Shopfloor; onClose: () => void }) {
  const f = pick.feature
  const cat = f?.category ?? pick.category
  return (
    <div className="fixed inset-x-0 bottom-0 z-20 max-h-[60vh] overflow-y-auto rounded-t-xl border-t border-slate-700 bg-slate-900 p-4 shadow-2xl"
      role="dialog" aria-label="Feature-details">
      <div className="mb-2 flex items-start gap-2">
        {cat && data.palette[cat] && (
          <span className="mt-1 h-4 w-4 shrink-0 rounded-sm" style={{ background: data.palette[cat].hex }} />
        )}
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-slate-100">{f ? f.label : CAM_LABEL[cat ?? ''] ?? 'Oppervlak'}</h2>
          <p className="text-xs text-slate-400">
            {f ? <>{f.id} · {f.type} · vlak {f.face ?? '-'}</> : 'Geen feature in de buurt'}
            {cat && <> · {CAM_LABEL[cat] ?? cat}</>}
          </p>
        </div>
        <button className="btn" onClick={onClose} aria-label="Sluiten">✕</button>
      </div>
      {f && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          {f.tolerance && <Row k="Tolerantie" v={f.tolerance} />}
          {f.ra_um != null && <Row k="Ruwheid" v={`Ra ${f.ra_um} µm`} />}
          {f.tools.map((t, i) => (
            <Row key={i} k={i === 0 ? 'Gereedschap' : ''} v={`${t.tool_id} ${t.tool_name} (${t.operation}, Ø${t.diameter_mm})`} />
          ))}
          {f.anchors.length > 1 && <Row k="Aantal" v={`${f.anchors.length}×`} />}
        </dl>
      )}
      {f?.issues.map((iss, i) => (
        <p key={i} className={`mt-2 rounded px-2 py-1 text-xs ${iss.status === 'warning' ? 'bg-amber-500/10 text-amber-200' : 'bg-sky-500/10 text-sky-200'}`}>
          {iss.message}{iss.suggested_fix && <> — {iss.suggested_fix}</>}
        </p>
      ))}
      <p className="mt-2 font-mono text-[11px] text-slate-500">
        X {pick.point[0].toFixed(2)} · Y {pick.point[1].toFixed(2)} · Z {pick.point[2].toFixed(2)} (G54)
      </p>
    </div>
  )
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt className="text-slate-400">{k}</dt>
      <dd className="font-mono text-slate-100">{v}</dd>
    </>
  )
}

function Centered({ text }: { text: string }) {
  return <div className="flex h-full items-center justify-center p-6 text-center text-sm text-slate-400">{text}</div>
}
