/** STEP intake (DRAWING_SPEC §3 route B): explains what editing the recognised PartSpec does. */
export default function StepImportNote({ complete }: { complete: boolean }) {
  return (
    <div className="rounded-md border border-sky-800/70 bg-sky-500/10 px-3 py-2 text-xs text-sky-100">
      <span className="font-medium">STEP-import</span> — het model is je eigen STEP-bestand (verschoven naar datum 0,0,0).{' '}
      {complete
        ? 'De herkende PartSpec beschrijft het volledig: wijzigingen in de spec bouwen een nieuw model.'
        : 'Niet alles is als feature herkend: wijzigingen in de spec passen alleen documentatie aan (draad, toleranties, tekening); de geometrie blijft die van het STEP.'}
    </div>
  )
}