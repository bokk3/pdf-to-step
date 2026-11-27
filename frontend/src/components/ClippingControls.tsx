import { useEffect, useMemo, useState } from 'react'
import * as THREE from 'three'

export type Axis = 'x' | 'y' | 'z'
const NORMAL: Record<Axis, [number, number, number]> = { x: [1, 0, 0], y: [0, 1, 0], z: [0, 0, 1] }

interface Props {
  bbox: { min: [number, number, number]; max: [number, number, number] }
  onChange: (plane: THREE.Plane | null) => void
}

/** v1.5 shopfloor: X / Y / Z section plane through the part (CAD coordinates, mm). */
export default function ClippingControls({ bbox, onChange }: Props) {
  const [axis, setAxis] = useState<Axis | null>(null)
  const [flip, setFlip] = useState(false)
  const i = axis ? { x: 0, y: 1, z: 2 }[axis] : 0
  const lo = bbox.min[i]
  const hi = bbox.max[i]
  const [pos, setPos] = useState((lo + hi) / 2)

  useEffect(() => setPos((lo + hi) / 2), [axis, lo, hi])

  const plane = useMemo(() => {
    if (!axis) return null
    const n = new THREE.Vector3(...NORMAL[axis]).multiplyScalar(flip ? 1 : -1)
    // keeps points where n·p + constant >= 0: default shows the part below / left of the cut
    return new THREE.Plane(n, flip ? -pos : pos)
  }, [axis, flip, pos])

  useEffect(() => onChange(plane), [plane, onChange])

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Doorsnede</span>
      {(['x', 'y', 'z'] as Axis[]).map((a) => (
        <button key={a} type="button" aria-pressed={axis === a}
          className={`btn font-mono ${axis === a ? 'btn-primary' : ''}`} onClick={() => setAxis(axis === a ? null : a)}>
          {a.toUpperCase()}
        </button>
      ))}
      {axis && (
        <>
          <input
            type="range"
            aria-label={`Positie van het snijvlak langs ${axis.toUpperCase()}`}
            className="h-11 min-w-[140px] flex-1 accent-sky-500"
            min={lo}
            max={hi}
            step={Math.max((hi - lo) / 400, 0.01)}
            value={pos}
            onChange={(e) => setPos(Number(e.target.value))}
          />
          <span className="w-20 text-right font-mono text-xs text-slate-300">{pos.toFixed(1)} mm</span>
          <button type="button" className="btn" onClick={() => setFlip((f) => !f)} title="Andere kant tonen">⇄</button>
        </>
      )}
    </div>
  )
}
