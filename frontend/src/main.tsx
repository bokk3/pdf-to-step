import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import BatchDashboard from './components/BatchDashboard'
import ShopfloorView from './components/ShopfloorView'
import ToolLibraryPage from './components/ToolLibraryPage'
import QuoteCalculatorPage from './pages/QuoteCalculatorPage'
import RevisionDiffPage from './pages/RevisionDiffPage'
import SettingsPage from './pages/SettingsPage'
import { usePath } from './router'
import './index.css'

function Root() {
  const path = usePath()
  const shop = path.match(/^\/shopfloor\/([a-f0-9]+)\/?$/)
  if (shop) return <ShopfloorView jobId={shop[1]} />
  const batch = path.match(/^\/batches(?:\/(b[a-f0-9]+))?\/?$/)
  if (batch) return <BatchDashboard batchId={batch[1] ?? null} />
  if (/^\/tools\/?$/.test(path)) return <ToolLibraryPage />
  const cmp = path.match(/^\/compare\/([a-f0-9]+)\/([a-f0-9]+)\/?$/)
  if (cmp) return <RevisionDiffPage oldId={cmp[1]} newId={cmp[2]} />
  const quote = path.match(/^\/quote\/([a-f0-9]+)\/?$/)
  if (quote) return <QuoteCalculatorPage jobId={quote[1]} />
  const settings = path.match(/^\/settings(?:\/(vises|rates))?\/?$/)
  if (settings) return <SettingsPage tab={(settings[1] as 'vises' | 'rates') ?? 'vises'} />
  return <App />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)