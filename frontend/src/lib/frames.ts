// Mirror of backend/app/schemas/frames.py: face frames of the v2.0 (right-handed table) and v1.x (projected,
// never mirrored) conventions. Used by the 2D preview to draw any spec in one coordinate system.
import type { Feature, PartSpec } from '../types/partSpec'

type V3 = [number, number, number]
export interface Frame {
  origin: V3
  u: V3
  v: V3
  n: V3
}

export const isLegacy = (spec: PartSpec) => spec.schema_version === '1.0' || spec.schema_version === '1.1'

export function stockBbox(spec: PartSpec): { lo: V3; hi: V3 } {
  const { length_x: L, width_y: W, thickness_z: T } = spec.stock
  const center = spec.datum_origin.startsWith('center')
  const x0 = center ? -L / 2 : 0
  const y0 = center ? -W / 2 : 0
  const z0 = spec.datum_origin.endsWith('top_face') ? -T : 0
  return { lo: [x0, y0, z0], hi: [x0 + L, y0 + W, z0 + T] }
}

const X: V3 = [1, 0, 0]
const Y: V3 = [0, 1, 0]
const Z: V3 = [0, 0, 1]
const neg = (a: V3): V3 => [-a[0], -a[1], -a[2]]

export function orthoFrames(spec: PartSpec, legacy = isLegacy(spec)): Record<string, Frame> {
  const {
    lo: [x0, y0, z0],
    hi: [x1, y1, z1],
  } = stockBbox(spec)
  if (legacy)
    return {
      top: { origin: [0, 0, z1], u: X, v: Y, n: Z },
      bottom: { origin: [0, 0, z0], u: X, v: Y, n: neg(Z) },
      front: { origin: [0, y0, 0], u: X, v: Z, n: neg(Y) },
      back: { origin: [0, y1, 0], u: X, v: Z, n: Y },
      left: { origin: [x0, 0, 0], u: Y, v: Z, n: neg(X) },
      right: { origin: [x1, 0, 0], u: Y, v: Z, n: X },
    }
  return {
    top: { origin: [0, 0, z1], u: X, v: Y, n: Z },
    bottom: { origin: [x0, y1, z0], u: X, v: neg(Y), n: neg(Z) },
    front: { origin: [x0, y0, z0], u: X, v: Z, n: neg(Y) },
    back: { origin: [x1, y1, z0], u: neg(X), v: Z, n: Y },
    left: { origin: [x0, y1, z0], u: neg(Y), v: Z, n: neg(X) },
    right: { origin: [x1, y0, z0], u: Y, v: Z, n: X },
  }
}

const dot = (a: V3, b: V3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
const point = (f: Frame, u: number, v: number): V3 => [0, 1, 2].map((i) => f.origin[i] + u * f.u[i] + v * f.v[i]) as V3
const local = (f: Frame, p: V3): [number, number] => {
  const d: V3 = [p[0] - f.origin[0], p[1] - f.origin[1], p[2] - f.origin[2]]
  return [dot(d, f.u), dot(d, f.v)]
}

/** Same geometry in the v1 projected coordinates (what the 2D preview draws). Tilted-plane features are dropped. */
export function toLegacyCoords(spec: PartSpec): PartSpec {
  if (isLegacy(spec)) return spec
  const from = orthoFrames(spec, false)
  const to = orthoFrames(spec, true)
  const features: Feature[] = []
  for (const f of spec.features) {
    const a = from[f.face]
    const b = to[f.face]
    if (!a || !b) continue // plane:<id>
    const sx = dot(a.u, b.u)
    const sy = dot(a.v, b.v)
    const m = (x: number, y: number): [number, number] => local(b, point(a, x, y))
    const g = structuredClone(f) as unknown as Record<string, unknown>
    if ('center_x' in g) {
      ;[g.center_x, g.center_y] = m(g.center_x as number, g.center_y as number)
      g.extra_positions = ((g.extra_positions as [number, number][] | undefined) ?? []).map(([x, y]) => m(x, y))
      const p = g.pattern as Record<string, number | string> | null | undefined
      if (p && p.type === 'linear') {
        p.pitch_x = sx * (p.pitch_x as number)
        p.pitch_y = sy * (p.pitch_y as number)
      } else if (p) {
        const t = ((p.start_angle_deg as number) * Math.PI) / 180
        p.start_angle_deg = (Math.atan2(sy * Math.sin(t), sx * Math.cos(t)) * 180) / Math.PI
        p.sweep_deg = sx * sy * (p.sweep_deg as number)
      }
    }
    if (typeof g.rotation_deg === 'number') g.rotation_deg = sx * sy * g.rotation_deg
    if (Array.isArray(g.points_2d)) g.points_2d = (g.points_2d as [number, number][]).map(([x, y]) => m(x, y))
    if ('start_x' in g) {
      ;[g.start_x, g.start_y] = m(g.start_x as number, g.start_y as number)
      ;[g.end_x, g.end_y] = m(g.end_x as number, g.end_y as number)
    }
    if ('x_min' in g) {
      const [ua, va] = m(g.x_min as number, g.y_min as number)
      const [ub, vb] = m(g.x_max as number, g.y_max as number)
      ;[g.x_min, g.x_max, g.y_min, g.y_max] = [Math.min(ua, ub), Math.max(ua, ub), Math.min(va, vb), Math.max(va, vb)]
    }
    features.push(g as unknown as Feature)
  }
  return { ...spec, schema_version: '1.1', features }
}
