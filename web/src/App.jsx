import { Suspense, lazy, useEffect, useState } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api } from './api'
import AppShell from './components/AppShell'
import SetupWizard from './components/SetupWizard'
import ErrorBoundary from './components/ErrorBoundary'
import { ErrorProvider } from './components/ErrorDialog'
import { HintsContext } from './hints'
import { ToastProvider } from './toast'
import { ConfirmProvider } from './confirm'
import { getGuideSeen, getHintsOn, setGuideSeen, setHintsOn } from './prefs'
import { isReady } from './ready'

// Every page is fetched on demand, so opening a route costs that route's code and nothing
// else. The shell, the providers and the setup wizard stay eager because they are on
// screen before any route has resolved.
//
// This began to matter when the phone console arrived. It imports three small local
// modules, but as one bundle it still shipped the reader, the photo-OCR tool, the glossary
// UI and the whole markdown stack — none of which a phone can even reach — just to draw
// three numbers, over a tailnet link on mobile data.
const LibraryPage = lazy(() => import('./pages/LibraryPage'))
const SeriesPage = lazy(() => import('./pages/SeriesPage'))
const SeriesDetailPage = lazy(() => import('./pages/SeriesDetailPage'))
const PostingPage = lazy(() => import('./pages/PostingPage'))
const PhonePage = lazy(() => import('./pages/PhonePage'))
const ImportPage = lazy(() => import('./pages/ImportPage'))
const ActivityPage = lazy(() => import('./pages/ActivityPage'))
const ArchivePage = lazy(() => import('./pages/ArchivePage'))
const ReviewPage = lazy(() => import('./pages/ReviewPage'))
const GuidePage = lazy(() => import('./pages/GuidePage'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))
const UpkeepPage = lazy(() => import('./pages/UpkeepPage'))
const ScansPage = lazy(() => import('./pages/ScansPage'))
const ProjectLayout = lazy(() => import('./components/ProjectLayout'))
const ChaptersPage = lazy(() => import('./pages/ChaptersPage'))
const PagesPage = lazy(() => import('./pages/PagesPage'))
const ProjectActivityPage = lazy(() => import('./pages/ProjectActivityPage'))
const GlossaryPage = lazy(() => import('./pages/GlossaryPage'))
const ConsistencyPage = lazy(() => import('./pages/ConsistencyPage'))
const ProjectSettingsPage = lazy(() => import('./pages/ProjectSettingsPage'))
const ReaderPage = lazy(() => import('./pages/ReaderPage'))

function FullPageLoading() {
  return <div className="flex min-h-screen items-center justify-center bg-page text-hint">Loading…</div>
}

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

  if (status === undefined) return <FullPageLoading />

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
      {/* The outermost boundary, and in practice only /phone reaches it: every route
          inside AppShell suspends against the one around its Outlet, which is what keeps
          the sidebar on screen while a page is being fetched. */}
      <Suspense fallback={<FullPageLoading />}>
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
      </Suspense>
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
