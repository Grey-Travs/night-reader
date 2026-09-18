import { useEffect, useState } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api } from './api'
import AppShell from './components/AppShell'
import SetupWizard from './components/SetupWizard'
import ProjectLayout from './components/ProjectLayout'
import LibraryPage from './pages/LibraryPage'
import SeriesPage from './pages/SeriesPage'
import SeriesDetailPage from './pages/SeriesDetailPage'
import PostingPage from './pages/PostingPage'
import PhonePage from './pages/PhonePage'
import ImportPage from './pages/ImportPage'
import ActivityPage from './pages/ActivityPage'
import ArchivePage from './pages/ArchivePage'
import ReviewPage from './pages/ReviewPage'
import GuidePage from './pages/GuidePage'
import SettingsPage from './pages/SettingsPage'
import ChaptersPage from './pages/ChaptersPage'
import PagesPage from './pages/PagesPage'
import ScansPage from './pages/ScansPage'
import ProjectActivityPage from './pages/ProjectActivityPage'
import GlossaryPage from './pages/GlossaryPage'
import ConsistencyPage from './pages/ConsistencyPage'
import ProjectSettingsPage from './pages/ProjectSettingsPage'
import ReaderPage from './pages/ReaderPage'
import ErrorBoundary from './components/ErrorBoundary'
import { ErrorProvider } from './components/ErrorDialog'
import UpkeepPage from './pages/UpkeepPage'
import { HintsContext } from './hints'
import { ToastProvider } from './toast'
import { ConfirmProvider } from './confirm'
import { getGuideSeen, getHintsOn, setGuideSeen, setHintsOn } from './prefs'
import { isReady } from './ready'

export default function App() {
  const [status, setStatus] = useState(undefined) // undefined = still loading
  const [forceSetup, setForceSetup] = useState(false)
  const [skipSetup, setSkipSetup] = useState(false)
  const [hintsOn, setHintsOnState] = useState(() => getHintsOn())
  const navigate = useNavigate()
  // The phone console is a page with no app around it, so two things that are right for
  // the desktop have to be switched off for it: the first-run guide redirect, and the
  // setup wizard. Both assume a big screen and a person who came here to configure
  // something; a phone opening the link came to press one button.
  const isPhoneRoute = useLocation().pathname.startsWith('/phone')

  function toggleHints(on) { setHintsOnState(on); setHintsOn(on) }

  useEffect(() => { api.status().then(setStatus).catch(() => setStatus({})) }, [])

  // Send the user to the guide the very first time (then never again unless asked).
  //
  // Never from /phone. "First time" is really "this browser has an empty localStorage",
  // which is exactly what a phone opening the link has — so without this guard the phone
  // link and the home-screen icon would both land on the desktop guide instead of the
  // page they name, and the one tap the icon exists to save would be spent going back.
  useEffect(() => {
    if (status === undefined) return
    if (isPhoneRoute) return
    if (isReady(status) && !getGuideSeen()) { setGuideSeen(); navigate('/guide') }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  if (status === undefined) {
    return <div className="flex min-h-screen items-center justify-center bg-page text-hint">Loading…</div>
  }

  const ready = isReady(status)

  const hints = { on: hintsOn, setOn: toggleHints }

  const body = (!isPhoneRoute && (forceSetup || (!ready && !skipSetup))) ? (
    <SetupWizard
      status={status}
      setStatus={setStatus}
      onDone={() => { setForceSetup(false); setSkipSetup(true) }}
    />
  ) : (
    <ErrorBoundary>
      <Routes>
        {/* Outside AppShell on purpose: no sidebar, no top bar, nothing of the desktop
            site. This is what the phone link and the home-screen icon open. */}
        <Route path="phone" element={<PhonePage />} />
        <Route element={<AppShell status={status} setStatus={setStatus} onSetup={() => setForceSetup(true)} />}>
          <Route index element={<LibraryPage />} />
          <Route path="series" element={<SeriesPage />} />
          <Route path="series/:sid" element={<SeriesDetailPage />} />
          <Route path="posting" element={<PostingPage />} />
          <Route path="import" element={<ImportPage />} />
          <Route path="activity" element={<ActivityPage />} />
          <Route path="review" element={<ReviewPage />} />
          <Route path="scans" element={<ScansPage />} />
          <Route path="upkeep" element={<UpkeepPage />} />
          <Route path="archive" element={<ArchivePage />} />
          <Route path="guide" element={<GuidePage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="novel/:pid" element={<ProjectLayout />}>
            <Route index element={<ChaptersPage />} />
            <Route path="pages" element={<PagesPage />} />
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
          <ErrorProvider>
            {body}
          </ErrorProvider>
        </ConfirmProvider>
      </ToastProvider>
    </HintsContext.Provider>
  )
}
