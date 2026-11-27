import { useEffect, useState } from 'react'

/** Minimal path router (history API); the backend serves index.html for every non-API path. */
export function navigate(path: string) {
  if (path === window.location.pathname + window.location.hash) return
  window.history.pushState(null, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

export function usePath(): string {
  const [path, setPath] = useState(window.location.pathname)
  useEffect(() => {
    const on = () => setPath(window.location.pathname)
    window.addEventListener('popstate', on)
    return () => window.removeEventListener('popstate', on)
  }, [])
  return path
}
