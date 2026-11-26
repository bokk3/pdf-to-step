import { useRef, useState } from 'react'
import { api, formatBytes, uploadKind, type UploadKind } from '../api/client'
import { useMediaQuery } from '../hooks/useMediaQuery'

interface Props {
  onUploaded: (jobId: string) => void
}

const KIND_LABEL: Record<UploadKind, string> = {
  pdf: 'PDF-tekening',
  dxf: 'DXF (lagen, geen AI)',
  step: 'STEP-model (geen AI)',
  sketch: 'schetsfoto',
}

/** PDF drawings, layered DXF, STEP models and photos of the A4 sketch template (DRAWING_SPEC.md §3). */
export default function UploadDropzone({ onUploaded }: Props) {
  const input = useRef<HTMLInputElement>(null)
  const camera = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const touch = useMediaQuery('(pointer: coarse)')

  async function upload(files: FileList | null) {
    const file = files?.[0]
    if (!file) return
    setError(null)
    const kind = uploadKind(file)
    if (!kind) {
      setError('Ondersteund: PDF, DXF, STEP / STP of een foto (JPEG, PNG) van het schetssjabloon')
      return
    }
    setBusy(`${KIND_LABEL[kind]} ${file.name} (${formatBytes(file.size)})`)
    try {
      onUploaded((await api.upload(file, kind)).job_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
      if (input.current) input.current.value = ''
      if (camera.current) camera.current.value = ''
    }
  }

  return (
    <div className="space-y-2">
      <div
        role="button"
        tabIndex={0}
        aria-label="Bestand uploaden: PDF, DXF, STEP of schetsfoto"
        onClick={() => input.current?.click()}
        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && input.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setOver(false)
          void upload(e.dataTransfer.files)
        }}
        className={`flex cursor-pointer flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed px-3 text-center transition ${
          touch ? 'py-6 active:bg-slate-900' : 'py-5'
        } ${over ? 'border-sky-400 bg-sky-500/10' : 'border-slate-700 hover:border-slate-500 hover:bg-slate-900'}`}
      >
        <input ref={input} type="file" className="hidden" onChange={(e) => void upload(e.target.files)}
          accept="application/pdf,.pdf,.dxf,.step,.stp,image/jpeg,image/png,image/webp" />
        <svg viewBox="0 0 24 24" className="h-7 w-7 text-sky-400" fill="none" stroke="currentColor" strokeWidth={1.6}>
          <path d="M12 16V4m0 0-4 4m4-4 4 4M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" strokeLinecap="round" />
        </svg>
        {busy ? (
          <span className="text-sm text-sky-300">Uploaden {busy}…</span>
        ) : touch ? (
          <>
            <span className="text-base font-semibold text-slate-100">Tekening of model kiezen</span>
            <span className="text-xs text-slate-400">PDF · DXF · STEP · schetsfoto · max 50 MB</span>
          </>
        ) : (
          <>
            <span className="text-sm font-medium text-slate-200">Sleep een PDF, DXF, STEP of schetsfoto hierheen</span>
            <span className="text-[11px] text-slate-400">of klik om te bladeren · max 50 MB</span>
          </>
        )}
        {error && <span className="mt-1 text-xs text-rose-400">{error}</span>}
      </div>
      <div className="grid grid-cols-2 gap-2">
        <button type="button" className={`btn justify-center ${touch ? 'min-h-11' : ''}`} disabled={!!busy}
          onClick={() => camera.current?.click()}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden
            strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 8h3l2-3h6l2 3h3v11H4zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z" />
          </svg>
          Foto van schets
        </button>
        <a className={`btn justify-center ${touch ? 'min-h-11' : ''}`} href="/api/templates/sketch-a4.pdf" target="_blank"
          rel="noreferrer" title="Printbaar A4-sjabloon met ArUco-hoekmerkers (100 % afdrukken)">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden
            strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 3h7l5 5v13H7zM14 3v5h5M12 11v6m0 0-2.5-2.5M12 17l2.5-2.5" />
          </svg>
          Schetssjabloon (A4)
        </a>
        <input ref={camera} type="file" accept="image/*" capture="environment" className="hidden"
          onChange={(e) => void upload(e.target.files)} />
      </div>
    </div>
  )
}
