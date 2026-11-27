import { useCallback, useEffect, useRef, useState } from 'react'
import { api, useBatchEvents, type BatchStatus } from '../api/client'
import { STATUS_LABEL } from './PipelineProgress'
import { navigate } from '../router'

/** v1.5 §2.5: multi-page customer PDF -> child jobs; live table + "download all ready STEPs". */
export default function BatchDashboard({ batchId }: { batchId: string | null }) {
  const [batches, setBatches] = useState<BatchStatus[]>([])
  const [batch, setBatch] = useState<BatchStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)

  const loadList = useCallback(() => api.listBatches().then(setBatches).catch(() => undefined), [])
  useEffect(() => void loadList(), [loadList])
  useEffect(() => {
    setBatch(null)
    if (batchId) api.getBatch(batchId).then(setBatch).catch((e: Error) => setError(e.message))
  }, [batchId])
  useBatchEvents(batchId, setBatch)

  const onFile = async (file: File | undefined) => {
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.uploadBatch(file)
      void loadList()
      navigate(`/batches/${r.batch_id}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      if (input.current) input.current.value = ''
    }
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Batches" subtitle="Meerbladige klant-PDF → één job per onderdeel" />
      <div className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <section className="panel flex flex-wrap items-center gap-3 p-3">
          <input ref={input} type="file" accept="application/pdf,.pdf" className="hidden"
            onChange={(e) => void onFile(e.target.files?.[0])} />
          <button className="btn btn-primary" disabled={busy} onClick={() => input.current?.click()}>
            {busy ? 'Splitsen…' : 'Meerbladige PDF uploaden'}
          </button>
          <p className="text-xs text-slate-400">
            Pagina's worden op tekeningnummer en bladaanduiding (Blad 1/2, Sheet 2 of 3) gegroepeerd.
          </p>
          {error && <p className="w-full text-sm text-rose-300">{error}</p>}
        </section>

        {batch && <BatchTable batch={batch} />}

        <section className="panel">
          <div className="panel-title">Eerdere batches</div>
          {batches.length === 0 ? (
            <p className="p-3 text-sm text-slate-500">Nog geen batches.</p>
          ) : (
            <ul className="divide-y divide-slate-800">
              {batches.map((b) => (
                <li key={b.batch_id}>
                  <button className={`flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-slate-800/50 ${b.batch_id === batchId ? 'bg-slate-800/60' : ''}`}
                    onClick={() => navigate(`/batches/${b.batch_id}`)}>
                    <span className="min-w-0 flex-1 truncate text-sm text-slate-200">{b.filename}</span>
                    <span className="font-mono text-xs text-slate-400">{b.ready_count}/{b.job_count} gereed</span>
                    <span className="text-xs text-slate-500">{new Date(b.created_at * 1000).toLocaleString('nl-NL')}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  )
}

function BatchTable({ batch }: { batch: BatchStatus }) {
  return (
    <section className="panel">
      <div className="panel-title">
        <span className="normal-case tracking-normal text-slate-200">{batch.filename}</span>
        <span className="normal-case tracking-normal">{batch.message ?? batch.status} · {Math.round(batch.progress * 100)}%</span>
        <a className={`btn btn-primary normal-case tracking-normal ${batch.ready_count ? '' : 'pointer-events-none opacity-40'}`}
          href={`/api/batches/${batch.batch_id}/download-zip`} aria-disabled={!batch.ready_count}>
          Download alle gereede STEP's (.zip)
        </a>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-[11px] uppercase tracking-wide text-slate-400">
            <tr>
              <th className="px-3 py-2">Onderdeelnr.</th>
              <th className="px-3 py-2">Pagina's</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">DFM</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {batch.jobs.map((j) => (
              <tr key={j.job_id}>
                <td className="px-3 py-2 font-mono text-slate-100">{j.part_number ?? j.filename}</td>
                <td className="px-3 py-2 font-mono text-xs text-slate-400">{j.pages.join(', ')}</td>
                <td className="px-3 py-2">
                  <span className={j.status === 'ready' ? 'text-emerald-300' : j.status === 'failed' ? 'text-rose-300' : 'text-sky-200'}>
                    {STATUS_LABEL[j.status] ?? j.status}
                  </span>
                  {j.running && <span className="ml-1 text-xs text-slate-500">{Math.round(j.progress * 100)}%</span>}
                </td>
                <td className="px-3 py-2 text-xs">
                  {j.dfm ? (
                    <span className={j.dfm.passed ? (j.dfm.warnings ? 'text-amber-200' : 'text-emerald-300') : 'text-rose-300'}>
                      {j.dfm.passed ? 'OK' : 'fout'}{j.dfm.warnings ? ` · ${j.dfm.warnings} waarsch.` : ''}
                    </span>
                  ) : <span className="text-slate-500">–</span>}
                </td>
                <td className="px-3 py-2 text-right">
                  <a className="btn" href={`/#job=${j.job_id}`}>Openen</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <header className="flex flex-wrap items-center gap-3 border-b border-slate-800 bg-slate-950 px-4 py-2">
      <a href="/" className="flex items-center gap-2" onClick={(e) => { e.preventDefault(); navigate('/') }}>
        <img src="/icon.svg" alt="" className="h-7 w-7 rounded-md" />
        <span className="text-sm font-semibold text-slate-100">PDF → STEP</span>
      </a>
      <span className="text-sm text-slate-300">/ {title}</span>
      {subtitle && <span className="text-[11px] text-slate-500">{subtitle}</span>}
      <NavLinks />
    </header>
  )
}

export function NavLinks() {
  const link = (href: string, label: string) => (
    <a href={href} className="btn" onClick={(e) => { e.preventDefault(); navigate(href) }}>{label}</a>
  )
  return (
    <nav className="ml-auto flex gap-1.5">
      {link('/batches', 'Batches')}
      {link('/tools', 'Gereedschap')}
    </nav>
  )
}
