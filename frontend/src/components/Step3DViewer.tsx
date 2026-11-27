import { GizmoHelper, GizmoViewport, Grid, OrbitControls, useGLTF } from '@react-three/drei'
import { Canvas, useThree } from '@react-three/fiber'
import { Component, Suspense, useEffect, useLayoutEffect, useMemo, useState, type ReactNode } from 'react'
import * as THREE from 'three'

// CAD convention: Z up.
THREE.Object3D.DEFAULT_UP.set(0, 0, 1)

interface Props {
  glbUrl?: string
  orthoUrl?: string
  bboxMax?: [number, number, number]
  floorZ?: number // grid height: under the part (the default datum puts the top face at Z = 0)
}

type Tab = '3d' | 'ortho'

export default function Step3DViewer({ glbUrl, orthoUrl, bboxMax, floorZ = 0 }: Props) {
  const [tab, setTab] = useState<Tab>('3d')
  const [wireframe, setWireframe] = useState(false)
  const [edges, setEdges] = useState(true)
  const [grid, setGrid] = useState(true)
  const [axes, setAxes] = useState(true)
  const [fitTick, setFitTick] = useState(0)
  const size = Math.max(...(bboxMax ?? [100, 100, 100]), 10)

  return (
    <section className="panel flex h-full min-h-0 flex-col">
      <div className="panel-title">
        <div className="flex gap-1 normal-case tracking-normal">
          <button className={`btn ${tab === '3d' ? 'btn-primary' : ''}`} onClick={() => setTab('3d')}>3D-model</button>
          <button className={`btn ${tab === 'ortho' ? 'btn-primary' : ''}`} onClick={() => setTab('ortho')} disabled={!orthoUrl}
            title="ortho_check.png: voor-, zij-, boven- en isoaanzicht">
            Aanzichten
          </button>
        </div>
        {tab === '3d' && (
          <div className="scroll-thin -mx-1 flex max-w-full items-center gap-1 overflow-x-auto px-1 normal-case tracking-normal">
            <Toggle on={wireframe} set={setWireframe} label="Draad" />
            <Toggle on={edges} set={setEdges} label="Randen" />
            <Toggle on={grid} set={setGrid} label="Raster" />
            <Toggle on={axes} set={setAxes} label="Assen" />
            <button className="btn" onClick={() => setFitTick((t) => t + 1)} title="Passend maken">⤢</button>
          </div>
        )}
      </div>
      <div className="relative min-h-0 flex-1">
        {tab === '3d' ? (
          glbUrl ? (
            <ErrorBoundary key={glbUrl} fallback={<Empty text="Geen 3D-voorbeeld voor deze versie" />}>
              <Canvas
                dpr={[1, 2]}
                camera={{ position: [size * 1.6, -size * 2, size * 1.4], fov: 35, near: 0.1, far: size * 200 }}
                gl={{ antialias: true }}
                className="!absolute inset-0"
              >
                <color attach="background" args={['#0b1220']} />
                <ambientLight intensity={0.45} />
                <hemisphereLight args={['#f1f5f9', '#334155', 1.0]} />
                <directionalLight position={[size * 2, -size * 3, size * 4]} intensity={1.6} />
                <directionalLight position={[-size * 3, size * 2, size]} intensity={0.6} />
                <OrbitControls makeDefault enableDamping dampingFactor={0.12} />
                <Suspense fallback={null}>
                  <Model url={glbUrl} wireframe={wireframe} edges={edges} fitTick={fitTick} />
                </Suspense>
                {grid && (
                  <Grid
                    position={[0, 0, floorZ - Math.max(size * 0.002, 0.05)]}
                    rotation={[Math.PI / 2, 0, 0]}
                    cellSize={size > 200 ? 10 : 5}
                    sectionSize={size > 200 ? 100 : 50}
                    cellColor="#1e293b"
                    sectionColor="#334155"
                    fadeDistance={size * 12}
                    infiniteGrid
                  />
                )}
                {axes && <axesHelper args={[size * 0.35]} />}
                <GizmoHelper alignment="bottom-right" margin={[64, 64]}>
                  <GizmoViewport axisColors={['#f43f5e', '#22c55e', '#3b82f6']} labelColor="white" />
                </GizmoHelper>
              </Canvas>
            </ErrorBoundary>
          ) : (
            <Empty text="Het 3D-voorbeeld verschijnt zodra de eerste STEP gecompileerd is" />
          )
        ) : (
          <a href={orthoUrl} target="_blank" rel="noreferrer" className="block h-full bg-white p-2">
            <img src={orthoUrl} alt="Orthografische controle-aanzichten" className="h-full w-full object-contain" />
          </a>
        )}
      </div>
    </section>
  )
}

function Model({ url, wireframe, edges, fitTick }: { url: string; wireframe: boolean; edges: boolean; fitTick: number }) {
  const { scene } = useGLTF(url)
  const { camera, controls, size, invalidate } = useThree()
  const root = useMemo(() => {
    const clone = scene.clone(true)
    clone.traverse((o) => {
      const mesh = o as THREE.Mesh
      if (!mesh.isMesh) return
      if (!mesh.geometry.getAttribute('normal')) mesh.geometry.computeVertexNormals()
      const colors = mesh.geometry.getAttribute('color') as THREE.BufferAttribute | undefined
      if (colors) srgbToLinear(colors) // CAM colours from model.step (v1.5 §2.2), stored as sRGB bytes
      mesh.material = new THREE.MeshStandardMaterial({
        color: colors ? '#ffffff' : '#b4c2d0',
        vertexColors: !!colors,
        metalness: 0.15,
        roughness: 0.5,
        side: THREE.DoubleSide,
        polygonOffset: true,
        polygonOffsetFactor: 1,
        polygonOffsetUnits: 1,
      })
      const lines = new THREE.LineSegments(
        new THREE.EdgesGeometry(mesh.geometry, 28),
        new THREE.LineBasicMaterial({ color: '#1e293b' }),
      )
      lines.name = '__edges'
      mesh.add(lines)
    })
    return clone
  }, [scene])

  useEffect(() => {
    root.traverse((o) => {
      const mesh = o as THREE.Mesh
      if (mesh.isMesh) (mesh.material as THREE.MeshStandardMaterial).wireframe = wireframe
      if (o.name === '__edges') o.visible = edges && !wireframe
    })
  }, [root, wireframe, edges])

  // GLB is Y-up (glTF convention); rotate back to the CAD Z-up frame.
  useLayoutEffect(() => {
    root.rotation.set(Math.PI / 2, 0, 0)
    root.updateWorldMatrix(true, true)
  }, [root])

  // Fit the camera to the part: measure AFTER the rotation is in the world matrix, and respect the
  // horizontal field of view so a portrait phone screen does not crop the part.
  useEffect(() => {
    const cam = camera as THREE.PerspectiveCamera
    root.updateWorldMatrix(true, true)
    const box = new THREE.Box3().setFromObject(root)
    if (box.isEmpty()) return
    const center = box.getCenter(new THREE.Vector3())
    const radius = box.getSize(new THREE.Vector3()).length() / 2
    const vfov = THREE.MathUtils.degToRad(cam.fov)
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * (size.width / Math.max(size.height, 1)))
    const dist = (radius / Math.sin(Math.min(vfov, hfov) / 2)) * 1.08
    cam.position.copy(center).addScaledVector(new THREE.Vector3(0.95, -1.25, 0.85).normalize(), dist)
    cam.near = Math.max(dist / 200, 0.01)
    cam.far = dist * 200
    cam.lookAt(center)
    cam.updateProjectionMatrix()
    const orbit = controls as unknown as { target: THREE.Vector3; update: () => void } | null
    if (orbit) {
      orbit.target.copy(center)
      orbit.update()
    }
    invalidate()
    // size: refit when the panel changes shape (tab switch, rotation)
  }, [root, camera, controls, size.width, size.height, fitTick, invalidate])

  return <primitive object={root} />
}

/** glTF COLOR_0 is linear; the exporter writes the palette's sRGB bytes. Convert once so the screen shows the exact
 *  CAM colour (e.g. royal blue #0066FF). Keeps the original sRGB copy in `userData.srgb` for colour lookups. */
export function srgbToLinear(attr: THREE.BufferAttribute) {
  if ((attr as unknown as { __linear?: boolean }).__linear) return
  const c = new THREE.Color()
  const srgb = new Float32Array(attr.count * 3)
  for (let i = 0; i < attr.count; i++) {
    c.setRGB(attr.getX(i), attr.getY(i), attr.getZ(i), THREE.SRGBColorSpace)
    srgb.set([attr.getX(i), attr.getY(i), attr.getZ(i)], i * 3)
    attr.setXYZ(i, c.r, c.g, c.b)
  }
  ;(attr as unknown as { __linear?: boolean; srgb?: Float32Array }).__linear = true
  ;(attr as unknown as { srgb?: Float32Array }).srgb = srgb
  attr.needsUpdate = true
}

function Toggle({ on, set, label }: { on: boolean; set: (v: boolean) => void; label: string }) {
  return (
    <button className={`btn ${on ? 'border-sky-600 text-sky-200' : 'text-slate-400'}`} onClick={() => set(!on)}>
      {label}
    </button>
  )
}

function Empty({ text }: { text: string }) {
  return <div className="flex h-full items-center justify-center p-6 text-center text-sm text-slate-500">{text}</div>
}

class ErrorBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  render() {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}
