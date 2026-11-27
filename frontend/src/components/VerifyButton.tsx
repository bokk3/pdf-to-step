import { useState } from 'react'
import { api } from '../api/client'
import type { JobDetail } from '../types/partSpec'

/** v1.5 §2.6 "Mark verified": logs the operator-confirmed PartSpec against the AI reading. */
export default function VerifyButton({ job, running, onDone }: { job: JobDetail; running: boolean; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  if (!job.current_version || job.status !== 'ready') return null
  const verified = !!job.verified_at
  return (
    <button
      className={`btn ${verified ? 'border-emerald-700 text-emerald-300' : ''}`}
      disabled={running || busy}
      title={verified ? `Gecontroleerd op ${new Date(job.verified_at! * 1000).toLocaleString('nl-NL')}; opnieuw markeren legt de huidige versie vast` : 'De huidige PartSpec is door de operator gecontroleerd'}
      onClick={async () => {
        setBusy(true)
        try {
          await api.verify(job.job_id)
          onDone()
        } finally {
          setBusy(false)
        }
      }}
    >
      {verified ? '✓ Gecontroleerd' : 'Markeer gecontroleerd'}
    </button>
  )
}
