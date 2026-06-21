import { useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { api } from './api'
import AppShell from './components/AppShell'
import SetupWizard from './components/SetupWizard'
import ProjectLayout from './components/ProjectLayout'
import LibraryPage from './pages/LibraryPage'
import ActivityPage from './pages/ActivityPage'
import ArchivePage from './pages/ArchivePage'
import ReviewPage from './pages/ReviewPage'
import GuidePage from './pages/GuidePage'
import SettingsPage from './pages/SettingsPage'
import ChaptersPage from './pages/ChaptersPage'
import ProjectActivityPage from './pages/ProjectActivityPage'
import GlossaryPage from './pages/GlossaryPage'
import ConsistencyPage from './pages/ConsistencyPage'
import ProjectSettingsPage from './pages/ProjectSettingsPage'
import ReaderPage from './pages/ReaderPage'
import ErrorBoundary from './components/ErrorBoundary'
import { HintsContext } from './hints'
import { ToastProvider } from './toast'
import { ConfirmProvider } from './confirm'
import { getGuideSeen, getHintsOn, setGuideSeen, setHintsOn } from './prefs'

export default function App() {
  const [status, setStatus] = useState(undefined) // undefined = still loading
  const [forceSetup, setForceSetup] = useState(false)
  const [skipSetup, setSkipSetup] = useState(false)
  const [hintsOn, setHintsOnState] = useState(() => getHintsOn())
  const navigate = useNavigate()

  function toggleHints(on) { setHintsOnState(on); setHintsOn(on) }

  useEffect(() => { api.status().then(setStatus).catch(() => setStatus({})) }, [])

  // Send the user to the guide the very first time (then never again unless asked).
  useEffect(() => {
    if (status === undefined) return
    const isReady = status.config_present && status.claude_logged_in &&
      status.google_client_secret_present && status.google_logged_in
    if (isReady && !getGuideSeen()) { setGuideSeen(); navigate('/guide') }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  if (status === undefined) {
    return <div className="flex min-h-screen items-center justify-center bg-page text-hint">Loading…</div>
  }

  const ready = status.config_present && status.claude_logged_in &&
    status.google_client_secret_present && status.google_logged_in

  const hints = { on: hintsOn, setOn: toggleHints }

  const body = (forceSetup || (!ready && !skipSetup)) ? (
    <SetupWizard
      status={status}
      setStatus={setStatus}
      onDone={() => { setForceSetup(false); setSkipSetup(true) }}
    />
  ) : (
    <ErrorBoundary>
      <Routes>
        <Route element={<AppShell status={status} setStatus={setStatus} onSetup={() => setForceSetup(true)} />}>
          <Route index element={<LibraryPage />} />
          <Route path="activity" element={<ActivityPage />} />
          <Route path="review" element={<ReviewPage />} />
          <Route path="archive" element={<ArchivePage />} />
          <Route path="guide" element={<GuidePage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="novel/:pid" element={<ProjectLayout />}>
            <Route index element={<ChaptersPage />} />
            <Route path="activity" element={<ProjectActivityPage />} />
            <Route path="glossary" element={<GlossaryPage />} />
            <Route path="consistency" element={<ConsistencyPage />} />
            <Route path="settings" element={<ProjectSettingsPage />} />
            <Route path="chapter/:idx" element={<ReaderPage />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ErrorBoundary>
  )

  return (
    <HintsContext.Provider value={hints}>
      <ToastProvider>
        <ConfirmProvider>
          {body}
        </ConfirmProvider>
      </ToastProvider>
    </HintsContext.Provider>
  )
}
