import { useRef, useState } from 'react'
import { api, formatBytes } from '../api/client'
import { useMediaQuery } from '../hooks/useMediaQuery'

interface Props {
  onUploaded: (jobId: string) => void
}

export default function UploadDropzone({ onUploaded }: Props) {
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const touch = useMediaQuery('(pointer: coarse)')

  async function upload(files: FileList | null) {
    const file = files?.[0]
    if (!file) return
    setError(null)
    if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') {
      setError('Alleen PDF-tekeningen worden ondersteund')
      return
    }
    setBusy(`${file.name} (${formatBytes(file.size)})`)
    try {
      onUploaded((await api.upload(file)).job_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
      if (input.current) input.current.value = ''
    }
  }

  return (
    <div
      role="button"
      tabIndex={0}
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
        touch ? 'py-7 active:bg-slate-900' : 'py-5'
      } ${over ? 'border-sky-400 bg-sky-500/10' : 'border-slate-700 hover:border-slate-500 hover:bg-slate-900'}`}
    >
      <input ref={input} type="file" accept="application/pdf,.pdf" className="hidden" onChange={(e) => void upload(e.target.files)} />
      <svg viewBox="0 0 24 24" className="h-7 w-7 text-sky-400" fill="none" stroke="currentColor" strokeWidth={1.6}>
        <path d="M12 16V4m0 0-4 4m4-4 4 4M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" strokeLinecap="round" />
      </svg>
      {busy ? (
        <span className="text-sm text-sky-300">Uploaden {busy}…</span>
      ) : touch ? (
        <>
          <span className="text-base font-semibold text-slate-100">PDF-tekening kiezen</span>
          <span className="text-xs text-slate-400">uit Bestanden, Drive of een e-mailbijlage · max 50 MB</span>
        </>
      ) : (
        <>
          <span className="text-sm font-medium text-slate-200">Sleep een PDF-tekening hierheen</span>
          <span className="text-[11px] text-slate-400">of klik om te bladeren · max 50 MB</span>
        </>
      )}
      {error && <span className="mt-1 text-xs text-rose-400">{error}</span>}
    </div>
  )
}
