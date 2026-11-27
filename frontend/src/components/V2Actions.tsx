import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { navigate } from '../router'
import type { JobDetail } from '../types/partSpec'
import SoftjawModal from './SoftjawModal'

/** v2.0 job actions: Op 2 softjaws, quote calculator and "compare with previous rev" (same part number). */
export default function V2Actions({ job, running, softjawsOpen, setSoftjawsOpen, full = false }: {
  job: JobDetail
  running: boolean
  softjawsOpen: boolean
  setSoftjawsOpen: (open: boolean) => void
  full?: boolean
}) {
  const [previous, setPrevious] = useState<{ id: string; rev: string | null } | null>(null)
  const built = !!job.artifacts['model.step'] && job.status === 'ready'

  useEffect(() => {
    setPrevious(null)
    if (!job.part_number) return
    api.partRevisions(job.part_number).then((r) => {
      const order = r.by_revision
      const i = order.indexOf(job.job_id)
      const prevId = i > 0 ? order[i - 1] : null
      const prev = r.revisions.find((x) => x.job_id === prevId && x.current_version > 0)
      setPrevious(prev ? { id: prev.job_id, rev: prev.revision } : null)
    }).catch(() => undefined)
  }, [job.job_id, job.part_number, job.revision])

  if (!built) return null
  const cls = `btn ${full ? 'w-full' : ''}`
  return (
    <>
      <button className={cls} disabled={running} onClick={() => setSoftjawsOpen(true)} title="Op 2: zachte bekken met dogbones (STEP)">
        Zachte bekken
      </button>
      <a className={cls} href={`/quote/${job.job_id}`} onClick={(e) => { e.preventDefault(); navigate(`/quote/${job.job_id}`) }}>
        Offerte
      </a>
      {previous && (
        <a className={`${cls} border-violet-700 text-violet-200`} href={`/compare/${previous.id}/${job.job_id}`}
          onClick={(e) => { e.preventDefault(); navigate(`/compare/${previous.id}/${job.job_id}`) }}
          title="3D-verschil en gewijzigde features t.o.v. de vorige revisie van dit tekeningnummer">
          Vergelijk met rev {previous.rev ?? 'vorige'}
        </a>
      )}
      {softjawsOpen && <SoftjawModal jobId={job.job_id} version={job.version} onClose={() => setSoftjawsOpen(false)} />}
    </>
  )
}
