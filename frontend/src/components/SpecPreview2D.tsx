import type { ReactElement } from 'react'
import type { Feature, PartSpec, Pattern } from '../types/partSpec'
import { needsReview } from '../types/partSpec'

// DRAWING_SPEC.md §7 phase 4: live 2D preview of the PartSpec being edited (first-angle: front view above the top
// view, shared X). Pure client-side SVG in datum millimetres, so it follows every keystroke without a rebuild.

type XY = [number, number]
const ok = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Same placement rule as PartSpec._Positioned.positions() in the backend. */
export function positions(f: { center_x: number; center_y: number; pattern?: Pattern | null; extra_positions?: XY[] }): XY[] {
  const base: XY = [f.center_x, f.center_y]
  const p = f.pattern
  let pts: XY[] = [base]
  if (p?.type === 'linear') {
    pts = []
    for (let j = 0; j < Math.max(1, p.count_y); j++)
      for (let i = 0; i < Math.max(1, p.count_x); i++) pts.push([base[0] + i * p.pitch_x, base[1] + j * p.pitch_y])
  } else if (p?.type === 'circular') {
    const full = Math.abs(Math.abs(p.sweep_deg) - 360) < 1e-9
    const step = p.sweep_deg / (full ? p.count : Math.max(p.count - 1, 1))
    pts = Array.from({ length: Math.max(1, p.count) }, (_, k) => {
      const a = ((p.start_angle_deg + k * step) * Math.PI) / 180
      return [base[0] + p.radius * Math.cos(a), base[1] + p.radius * Math.sin(a)] as XY
    })
  }
  return [...pts, ...(f.extra_positions ?? [])]
}

export function stockBox(spec: PartSpec): { x0: number; y0: number; z0: number; x1: number; y1: number; z1: number } {
  const { length_x: L, width_y: W, thickness_z: T } = spec.stock
  const center = spec.datum_origin.startsWith('center')
  const x0 = center ? -L / 2 : 0
  const y0 = center ? -W / 2 : 0
  const z0 = spec.datum_origin.endsWith('top_face') ? -T : 0
  return { x0, y0, z0, x1: x0 + L, y1: y0 + W, z1: z0 + T }
}

function xExtent(f: Feature): [number, number][] {
  switch (f.type) {
    case 'hole': {
      const r = Math.max(f.diameter, f.counterbore?.diameter ?? 0, f.countersink?.diameter ?? 0) / 2
      return positions(f).map(([x]) => [x - r, x + r])
    }
    case 'pocket_rect':
    case 'boss_rect': {
      const a = ((f.rotation_deg ?? 0) * Math.PI) / 180
      const hx = (Math.abs(Math.cos(a)) * f.length_x + Math.abs(Math.sin(a)) * f.width_y) / 2
      return positions(f).map(([x]) => [x - hx, x + hx])
    }
    case 'pocket_circle':
    case 'boss_circle':
      return positions(f).map(([x]) => [x - f.diameter / 2, x + f.diameter / 2])
    case 'slot':
      return [[Math.min(f.start_x, f.end_x) - f.width / 2, Math.max(f.start_x, f.end_x) + f.width / 2]]
    case 'pocket_polygon':
      return [[Math.min(...f.points_2d.map((p) => p[0])), Math.max(...f.points_2d.map((p) => p[0]))]]
    case 'step_cut':
      return [[f.x_min, f.x_max]]
    default:
      return []
  }
}

interface Props {
  spec: PartSpec
  selectedId: string | null
  onSelect: (id: string) => void
  /** max rendered height in CSS px */
  height?: number
}

export default function SpecPreview2D({ spec, selectedId, onSelect, height = 260 }: Props) {
  const { length_x: L, width_y: W, thickness_z: T } = spec.stock
  if (![L, W, T].every((v) => ok(v) && v > 0)) {
    return <div className="p-3 text-xs text-slate-500">Vul de stockmaten in voor het 2D-voorbeeld.</div>
  }
  const b = stockBox(spec)
  const m = Math.max(L, W) * 0.06 + 4 // margin
  const gap = Math.max(6, Math.max(L, W) * 0.08)
  const fz = Math.max(L, W) / 38 // label size in mm
  const vw = L + 2 * m
  const vh = T + gap + W + 2 * m + fz * 1.6
  // view origins (SVG y grows downwards)
  const X = (x: number) => m + (x - b.x0)
  const FZ = (z: number) => m + fz * 1.6 + (b.z1 - z) // front view
  const TY = (y: number) => m + fz * 1.6 + T + gap + (b.y1 - y) // top view
  const sw = Math.max(L, W) / 400 // base stroke width (mm)

  const prof = spec.outer_profile
  const rr = prof.type === 'rounded_rectangle' ? Math.min(prof.corner_radius, L / 2, W / 2) : 0

  const featureShapes = (f: Feature) => {
    const sel = f.id === selectedId
    const review = needsReview(f.confidence)
    const hidden = f.face === 'bottom'
    const stroke = sel ? '#38bdf8' : review ? '#fbbf24' : '#7dd3fc'
    const common = {
      stroke, strokeWidth: sw * (sel ? 2.6 : 1.4), fill: sel ? 'rgba(56,189,248,0.18)' : 'rgba(125,211,252,0.06)',
      strokeDasharray: hidden ? `${sw * 6} ${sw * 3}` : undefined, vectorEffect: 'non-scaling-stroke' as const,
    }
    const top: ReactElement[] = []
    const front: ReactElement[] = []
    const depthOf = (fe: Feature): number | null => {
      if ('through_all' in fe && fe.through_all) return T
      if ('depth' in fe && ok(fe.depth)) return fe.depth
      return null
    }
    if (f.face === 'top' || f.face === 'bottom') {
      switch (f.type) {
        case 'hole':
          positions(f).forEach(([x, y], i) => {
            if (!ok(x) || !ok(y)) return
            const seat = f.counterbore?.diameter ?? f.countersink?.diameter
            if (seat) top.push(<circle key={`s${i}`} cx={X(x)} cy={TY(y)} r={seat / 2} {...common} fill="none" />)
            top.push(<circle key={`h${i}`} cx={X(x)} cy={TY(y)} r={f.diameter / 2} {...common}
              strokeDasharray={f.thread ? `${sw * 4} ${sw * 2}` : common.strokeDasharray} />)
          })
          break
        case 'pocket_rect':
        case 'boss_rect':
          positions(f).forEach(([x, y], i) => ok(x) && ok(y) && top.push(
            <rect key={i} x={X(x) - f.length_x / 2} y={TY(y) - f.width_y / 2} width={f.length_x} height={f.width_y}
              rx={f.corner_radius} transform={`rotate(${-(f.rotation_deg ?? 0)} ${X(x)} ${TY(y)})`} {...common} />))
          break
        case 'pocket_circle':
        case 'boss_circle':
          positions(f).forEach(([x, y], i) => ok(x) && ok(y) && top.push(
            <circle key={i} cx={X(x)} cy={TY(y)} r={f.diameter / 2} {...common} />))
          break
        case 'slot': {
          const len = Math.hypot(f.end_x - f.start_x, f.end_y - f.start_y)
          const ang = (Math.atan2(f.end_y - f.start_y, f.end_x - f.start_x) * 180) / Math.PI
          const cx = (f.start_x + f.end_x) / 2
          const cy = (f.start_y + f.end_y) / 2
          if ([len, cx, cy, f.width].every(ok))
            top.push(<rect key="s" x={X(cx) - (len + f.width) / 2} y={TY(cy) - f.width / 2} width={len + f.width}
              height={f.width} rx={f.width / 2} transform={`rotate(${-ang} ${X(cx)} ${TY(cy)})`} {...common} />)
          break
        }
        case 'pocket_polygon':
          if (f.points_2d.length >= 3)
            top.push(<polygon key="p" points={f.points_2d.map(([x, y]) => `${X(x)},${TY(y)}`).join(' ')} {...common} />)
          break
        case 'step_cut':
          top.push(<rect key="st" x={X(f.x_min)} y={TY(f.y_max)} width={f.x_max - f.x_min} height={f.y_max - f.y_min}
            rx={f.corner_radius} {...common} />)
          break
        case 'complex_3d':
          if (f.bbox_min && f.bbox_max)
            top.push(<rect key="c" x={X(f.bbox_min[0])} y={TY(f.bbox_max[1])} width={f.bbox_max[0] - f.bbox_min[0]}
              height={f.bbox_max[1] - f.bbox_min[1]} {...common} strokeDasharray={`${sw * 2} ${sw * 2}`} />)
          break
      }
      // front view: hidden outline of the cut depth (bosses: solid, above the face)
      const d = f.type === 'step_cut' ? f.depth : f.type === 'boss_rect' || f.type === 'boss_circle' ? -f.height : depthOf(f)
      if (d != null && ok(d)) {
        const fromTop = f.face === 'top'
        const zFace = fromTop ? b.z1 : b.z0
        const zEnd = fromTop ? zFace - d : zFace + d
        xExtent(f).forEach(([xa, xb], i) => {
          if (!ok(xa) || !ok(xb)) return
          const isBoss = d < 0
          front.push(<rect key={`f${i}`} x={X(xa)} y={FZ(Math.max(zFace, zEnd))} width={xb - xa} height={Math.abs(zEnd - zFace)}
            {...common} fill={isBoss ? common.fill : 'none'} strokeDasharray={isBoss ? undefined : `${sw * 5} ${sw * 2.5}`} />)
        })
      }
    } else if ((f.face === 'front' || f.face === 'back') && 'center_x' in f) {
      // features on the front / back face appear in the front view (x = X, center_y = Z)
      const dash = f.face === 'back' ? `${sw * 6} ${sw * 3}` : undefined
      positions(f).forEach(([x, z], i) => {
        if (!ok(x) || !ok(z)) return
        if (f.type === 'hole' || f.type === 'pocket_circle' || f.type === 'boss_circle')
          front.push(<circle key={`ff${i}`} cx={X(x)} cy={FZ(z)} r={f.diameter / 2} {...common} strokeDasharray={dash} />)
        else if (f.type === 'pocket_rect' || f.type === 'boss_rect')
          front.push(<rect key={`ff${i}`} x={X(x) - f.length_x / 2} y={FZ(z) - f.width_y / 2} width={f.length_x}
            height={f.width_y} rx={f.corner_radius} {...common} strokeDasharray={dash} />)
      })
    }
    const anchor = top.length ? firstPoint(f) : null
    return { top, front, anchor, sel }
  }

  const shapes = spec.features.map((f) => ({ f, ...featureShapes(f) }))
  const outline = { stroke: '#e2e8f0', strokeWidth: sw * 2, fill: 'rgba(148,163,184,0.06)', vectorEffect: 'non-scaling-stroke' as const }

  return (
    <figure className="m-0">
      <svg viewBox={`0 0 ${vw} ${vh}`} role="img" aria-label={`2D-voorbeeld ${L} x ${W} x ${T} mm`}
        style={{ maxHeight: height }} className="mx-auto block h-auto w-full select-none">
        <text x={m} y={m + fz} fontSize={fz} fill="#94a3b8">VOORAANZICHT</text>
        <rect x={X(b.x0)} y={FZ(b.z1)} width={L} height={T} {...outline} />
        <text x={m} y={TY(b.y1) - fz * 0.5} fontSize={fz} fill="#94a3b8">BOVENAANZICHT</text>
        {prof.type === 'polygon' && prof.points_2d && prof.points_2d.length >= 3 ? (
          <polygon points={prof.points_2d.map(([x, y]) => `${X(x)},${TY(y)}`).join(' ')} {...outline} />
        ) : (
          <rect x={X(b.x0)} y={TY(b.y1)} width={L} height={W} rx={rr} {...outline} />
        )}
        {shapes.map(({ f, top, front }) => (
          <g key={f.id} role="button" tabIndex={0} aria-label={`feature ${f.id}`} style={{ cursor: 'pointer' }}
            onClick={() => onSelect(f.id)} onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onSelect(f.id)}>
            {front}
            {top}
          </g>
        ))}
        {shapes.map(({ f, anchor, sel }) => anchor && (
          <text key={`l-${f.id}`} x={X(anchor[0]) + fz * 0.4} y={TY(anchor[1]) - fz * 0.4} fontSize={fz * 0.85}
            fill={sel ? '#38bdf8' : '#cbd5e1'} style={{ pointerEvents: 'none' }}>{f.id}</text>
        ))}
        {/* datum (0,0) */}
        {ok(b.x0) && (
          <g stroke="#f87171" strokeWidth={sw * 1.5} vectorEffect="non-scaling-stroke">
            <circle cx={X(0)} cy={TY(0)} r={fz * 0.35} fill="none" />
            <line x1={X(0)} y1={TY(0)} x2={X(0) + fz * 1.8} y2={TY(0)} />
            <line x1={X(0)} y1={TY(0)} x2={X(0)} y2={TY(0) - fz * 1.8} />
          </g>
        )}
      </svg>
      <figcaption className="mt-1 text-center text-[11px] text-slate-500">
        {L} × {W} × {T} mm · eerste-hoekprojectie · klik een vorm om de feature te kiezen · stippellijn = verborgen / onderkant
      </figcaption>
    </figure>
  )
}

function firstPoint(f: Feature): XY | null {
  if ('center_x' in f) return [f.center_x, f.center_y]
  if (f.type === 'slot') return [f.start_x, f.start_y]
  if (f.type === 'pocket_polygon') return f.points_2d[0] ?? null
  if (f.type === 'step_cut') return [f.x_min, f.y_max]
  return null
}
