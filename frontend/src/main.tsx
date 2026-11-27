import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import BatchDashboard from './components/BatchDashboard'
import ShopfloorView from './components/ShopfloorView'
import ToolLibraryPage from './components/ToolLibraryPage'
import { usePath } from './router'
import './index.css'

function Root() {
  const path = usePath()
  const shop = path.match(/^\/shopfloor\/([a-f0-9]+)\/?$/)
  if (shop) return <ShopfloorView jobId={shop[1]} />
  const batch = path.match(/^\/batches(?:\/(b[a-f0-9]+))?\/?$/)
  if (batch) return <BatchDashboard batchId={batch[1] ?? null} />
  if (/^\/tools\/?$/.test(path)) return <ToolLibraryPage />
  return <App />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)