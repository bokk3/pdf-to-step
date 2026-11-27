import { useEffect, useRef, useState } from 'react'
import { api, type MachineType, type QuoteBreakdown } from '../api/client'
import { PageHeader } from '../components/BatchDashboard'

const fmtMoney = (v: number, cur: string) =>
  new Intl.NumberFormat('nl-NL', { style: 'currency', currency: cur, maximumFractionDigits: 2 }).format(v)

/** v2.0 §3.4: live, deterministic price breaks (3-axis vs 5-axis 3+2) with a traceable breakdown + PDF / CSV. */
export default function QuoteCalculatorPage({ jobId }: { jobId: string }) {
  const [qtyText, setQtyText] = useState('1, 5, 10, 25, 50')
  const [custom, setCustom] = useState<number | ''>('')
  const [margin, setMargin] = useState<number | null>(null)
  const [softjaws, setSoftjaws] = useState<'auto' | 'yes' | 'no'>('auto')
  const [machine, setMachine] = useState<MachineType>('3axis_vmc')
  const [quote, setQuote] = useState<QuoteBreakdown | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const seq = useRef(0)

  const quantities = qtyText.split(/[,;\s]+/).map(Number).filter((n) => Number.isInteger(n) && n > 0).slice(0, 20)

  useEffect(() => {
    const my = ++seq.current
    const t = window.setTimeout(() => {
      setBusy(true)
      api.quote(jobId, {
        quantities: quantities.length ? quantities : [1],
        custom_quantity: custom === '' ? null : custom,
        machine_type: machine,
        include_softjaws: softjaws === 'auto' ? null : softjaws === 'yes',
        margin_pct: margin,
      }).then((q) => {
        if (my !== seq.current) return
        setQuote(q)
        setError(null)
        if (margin === null) setMargin(q.margin_pct)
      }).catch((e: Error) => my === seq.current && setError(e.message))
        .finally(() => my === seq.current && setBusy(false))
    }, 300)
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, qtyText, custom, margin, softjaws, machine])

  const cur = quote?.currency ?? 'EUR'
  const machines = quote ? (Object.keys(quote.machines) as MachineType[]) : []
  const qtys = quote ? quote.machines[machines[0]].price_breaks.map((b) => b.quantity) : []

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Offerte" subtitle={quote ? `job ${jobId} · v${quote.version} · ${quote.material_group}` : undefined} />
      <div className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <section className="panel grid gap-3 p-3 sm:grid-cols-2 lg:grid-cols-5">
          <label className="block lg:col-span-2">
            <span className="field-label">Aantallen</span>
            <input className="txt-input w-full" value={qtyText} onChange={(e) => setQtyText(e.target.value)} inputMode="numeric" />
          </label>
          <label className="block">
            <span className="field-label">Eigen aantal</span>
            <input className="num-input" type="number" min={1} value={custom}
              onChange={(e) => setCustom(e.target.value === '' ? '' : Math.max(1, Math.round(Number(e.target.value))))} />
          </label>
          <label className="block">
            <span className="field-label">Marge {margin ?? '–'} %</span>
            <input type="range" min={0} max={60} step={1} value={margin ?? 20} className="h-11 w-full accent-sky-500"
              onChange={(e) => setMargin(Number(e.target.value))} />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="field-label">Zachte bekken</span>
              <select className="sel-input w-full" value={softjaws} onChange={(e) => setSoftjaws(e.target.value as 'auto' | 'yes' | 'no')}>
                <option value="auto">auto</option>
                <option value="yes">ja</option>
                <option value="no">nee</option>
              </select>
            </label>
            <label className="block">
              <span className="field-label">Machine</span>
              <select className="sel-input w-full" value={machine} onChange={(e) => setMachine(e.target.value as MachineType)}>
                <option value="3axis_vmc">3-assig</option>
                <option value="5axis_3plus2">5-assig 3+2</option>
              </select>
            </label>
          </div>
        </section>
        {error && <p className="text-sm text-rose-300">{error}</p>}
        {quote && (
          <>
            <section className="panel overflow-x-auto">
              <div className="panel-title">
                <span>Staffel {busy && <span className="normal-case text-slate-500">· bijwerken…</span>}</span>
                <span className="flex gap-1.5 normal-case tracking-normal">
                  <a className="btn btn-primary" href={quote.exports.pdf} target="_blank" rel="noreferrer">PDF</a>
                  <a className="btn" href={quote.exports.csv}>CSV</a>
                </span>
              </div>
              <table className="w-full min-w-[560px] text-sm">
                <thead className="text-left text-[11px] uppercase tracking-wide text-slate-400">
                  <tr>
                    <th className="px-3 py-2">Stuks</th>
                    {machines.map((m) => (
                      <th key={m} className={`px-3 py-2 ${m === machine ? 'text-sky-300' : ''}`} colSpan={2}>
                        {quote.machines[m].label}{quote.cheaper === m && ' · goedkoopst'}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800 font-mono">
                  {qtys.map((q) => (
                    <tr key={q}>
                      <td className="px-3 py-1.5 text-slate-200">{q}</td>
                      {machines.map((m) => {
                        const b = quote.machines[m].price_breaks.find((x) => x.quantity === q)!
                        return [
                          <td key={`${m}u`} className={`px-3 py-1.5 ${m === machine ? 'text-slate-100' : 'text-slate-400'}`}
                            title={`kostprijs ${fmtMoney(b.unit_cost, cur)} · instelling ${fmtMoney(b.setup_share, cur)}/st`}>
                            {fmtMoney(b.unit_price, cur)}/st
                          </td>,
                          <td key={`${m}t`} className="px-3 py-1.5 text-slate-400">{fmtMoney(b.total_price, cur)}</td>,
                        ]
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
            <div className="grid gap-3 lg:grid-cols-2">
              {machines.map((m) => {
                const mq = quote.machines[m]
                return (
                  <section key={m} className={`panel ${m === machine ? 'border-sky-800' : ''}`}>
                    <div className="panel-title">
                      <span>{mq.label}</span>
                      <span className="normal-case tracking-normal text-slate-400">{mq.setups} opspanning(en) · {fmtMoney(mq.hourly_rate, cur)}/h</span>
                    </div>
                    <dl className="divide-y divide-slate-800 text-sm">
                      {[...quote.material_lines, ...mq.lines].map((ln) => (
                        <div key={ln.key} className="grid grid-cols-[1fr_auto] gap-x-3 px-3 py-1.5">
                          <dt className="text-slate-300">{ln.label}</dt>
                          <dd className="text-right font-mono text-slate-100">
                            {ln.unit === 'min' ? `${ln.value.toFixed(2)} min` : fmtMoney(ln.value, cur)}
                          </dd>
                          <dd className="col-span-2 font-mono text-[11px] text-slate-500">{ln.formula}</dd>
                        </div>
                      ))}
                    </dl>
                  </section>
                )
              })}
            </div>
            {quote.notes.length > 0 && (
              <ul className="space-y-1 text-xs text-amber-200">{quote.notes.map((n) => <li key={n}>• {n}</li>)}</ul>
            )}
            <p className="text-[11px] text-slate-500">
              Deterministisch: elke euro volgt uit volumes, B-Rep-oppervlakken, de gatenlijst, het opspanplan en de tarieven in
              Instellingen → Tarieven. Geen AI-schatting.
            </p>
          </>
        )}
      </div>
    </div>
  )
}
