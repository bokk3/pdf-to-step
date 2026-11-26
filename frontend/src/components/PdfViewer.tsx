import * as pdfjs from 'pdfjs-dist'
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist'
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

pdfjs.GlobalWorkerOptions.workerSrc = workerSrc

const MAX_CANVAS_PIXELS = 36_000_000

interface Props {
  url: string
  /** replaces the "Tekening" title, e.g. an Origineel / ISO toggle */
  header?: ReactNode
}

/** Zoom/pan PDF sheet viewer. Ctrl/⌘ + wheel or the buttons zoom; drag to pan. */
export default function PdfViewer({ url, header }: Props) {
  const scroller = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null)
  const [pageNo, setPageNo] = useState(1)
  const [zoom, setZoom] = useState(1) // 1 = fit width
  const [error, setError] = useState<string | null>(null)
  const [containerW, setContainerW] = useState(600)
  const drag = useRef<{ x: number; y: number; sl: number; st: number } | null>(null)
  const touches = useRef(new Map<number, { x: number; y: number }>())
  const pinch = useRef<{ d0: number; w: number; h: number; ratio: number } | null>(null)

  useEffect(() => {
    let cancelled = false
    setDoc(null)
    setError(null)
    setPageNo(1)
    setZoom(1)
    const task = pdfjs.getDocument({ url })
    task.promise.then(
      (d) => !cancelled && setDoc(d),
      (e: unknown) => !cancelled && setError(String(e)),
    )
    return () => {
      cancelled = true
      void task.destroy()
    }
  }, [url])

  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    const ro = new ResizeObserver(() => setContainerW(el.clientWidth))
    ro.observe(el)
    setContainerW(el.clientWidth)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    if (!doc || !canvas.current) return
    let task: RenderTask | null = null
    let cancelled = false
    void (async () => {
      const page = await doc.getPage(pageNo)
      if (cancelled || !canvas.current) return
      const base = page.getViewport({ scale: 1 })
      const cssScale = (Math.max(containerW - 16, 100) / base.width) * zoom
      let pxScale = cssScale * (window.devicePixelRatio || 1)
      const pixels = base.width * base.height * pxScale * pxScale
      if (pixels > MAX_CANVAS_PIXELS) pxScale *= Math.sqrt(MAX_CANVAS_PIXELS / pixels)
      const vp = page.getViewport({ scale: pxScale })
      const c = canvas.current
      c.width = Math.floor(vp.width)
      c.height = Math.floor(vp.height)
      c.style.width = `${Math.floor(base.width * cssScale)}px`
      c.style.height = `${Math.floor(base.height * cssScale)}px`
      const ctx = c.getContext('2d')
      if (!ctx) return
      task = page.render({ canvas: c, canvasContext: ctx, viewport: vp })
      try {
        await task.promise
      } catch {
        /* cancelled */
      }
    })()
    return () => {
      cancelled = true
      task?.cancel()
    }
  }, [doc, pageNo, zoom, containerW])

  const zoomBy = useCallback((factor: number) => {
    const el = scroller.current
    setZoom((z) => {
      const nz = Math.min(12, Math.max(0.25, z * factor))
      if (el) {
        // keep the view centre stable
        const cx = (el.scrollLeft + el.clientWidth / 2) / el.scrollWidth
        const cy = (el.scrollTop + el.clientHeight / 2) / el.scrollHeight
        requestAnimationFrame(() => {
          el.scrollLeft = cx * el.scrollWidth - el.clientWidth / 2
          el.scrollTop = cy * el.scrollHeight - el.clientHeight / 2
        })
      }
      return nz
    })
  }, [])

  function endPointer(id: number) {
    drag.current = null
    touches.current.delete(id)
    const p = pinch.current
    if (p && touches.current.size < 2) {
      pinch.current = null
      if (Math.abs(p.ratio - 1) > 0.02) zoomBy(p.ratio)
    }
  }

  useEffect(() => {
    const el = scroller.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomBy])

  return (
    <section className="panel flex h-full min-h-0 flex-col">
      <div className="panel-title">
        {header ?? <span>Tekening</span>}
        <div className="flex items-center gap-1 normal-case tracking-normal">
          {doc && doc.numPages > 1 && (
            <select className="sel-input" value={pageNo} onChange={(e) => setPageNo(Number(e.target.value))}>
              {Array.from({ length: doc.numPages }, (_, i) => (
                <option key={i} value={i + 1}>
                  pagina {i + 1}/{doc.numPages}
                </option>
              ))}
            </select>
          )}
          <button className="btn" onClick={() => zoomBy(1 / 1.25)} aria-label="Uitzoomen">−</button>
          <span className="w-11 text-center font-mono text-[11px] text-slate-400">{Math.round(zoom * 100)}%</span>
          <button className="btn" onClick={() => zoomBy(1.25)} aria-label="Inzoomen (Ctrl + scroll of knijpen)">+</button>
          <button className="btn" onClick={() => setZoom(1)} aria-label="Paginabreedte">⤢</button>
          <a className="btn" href={url} target="_blank" rel="noreferrer" aria-label="PDF openen">↗</a>
        </div>
      </div>
      <div
        ref={scroller}
        // one finger scrolls natively; two fingers pinch-zoom the drawing (not the page)
        style={{ touchAction: 'pan-x pan-y' }}
        className="scroll-thin relative min-h-0 flex-1 cursor-grab overflow-auto overscroll-contain bg-slate-200 p-2 active:cursor-grabbing"
        onPointerDown={(e) => {
          const el = scroller.current!
          if (e.pointerType === 'touch') {
            touches.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
            if (touches.current.size === 2 && canvas.current) {
              const [a, b] = [...touches.current.values()]
              pinch.current = {
                d0: Math.hypot(a.x - b.x, a.y - b.y),
                w: parseFloat(canvas.current.style.width) || canvas.current.clientWidth,
                h: parseFloat(canvas.current.style.height) || canvas.current.clientHeight,
                ratio: 1,
              }
            }
            return
          }
          if (e.pointerType !== 'mouse') return
          drag.current = { x: e.clientX, y: e.clientY, sl: el.scrollLeft, st: el.scrollTop }
          el.setPointerCapture(e.pointerId)
        }}
        onPointerMove={(e) => {
          if (e.pointerType === 'touch') {
            if (!touches.current.has(e.pointerId)) return
            touches.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
            const p = pinch.current
            if (p && touches.current.size === 2 && canvas.current) {
              const [a, b] = [...touches.current.values()]
              const z = Math.min(12, Math.max(0.25, zoom * (Math.hypot(a.x - b.x, a.y - b.y) / p.d0))) / zoom
              p.ratio = z
              canvas.current.style.width = `${p.w * z}px` // live preview, re-rendered sharp on release
              canvas.current.style.height = `${p.h * z}px`
            }
            return
          }
          const d = drag.current
          if (!d) return
          const el = scroller.current!
          el.scrollLeft = d.sl - (e.clientX - d.x)
          el.scrollTop = d.st - (e.clientY - d.y)
        }}
        onPointerUp={(e) => endPointer(e.pointerId)}
        onPointerCancel={(e) => endPointer(e.pointerId)}
      >
        {error && <div className="p-4 text-sm text-rose-600">PDF kon niet geladen worden: {error}</div>}
        {!doc && !error && <div className="p-4 text-sm text-slate-500">Tekening laden…</div>}
        <canvas ref={canvas} className="mx-auto block bg-white shadow-lg" />
      </div>
    </section>
  )
}
