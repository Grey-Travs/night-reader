import { useEffect, useState } from 'react'
import { api } from './api'
import ProjectLibrary from './components/ProjectLibrary'
import ProjectView from './components/ProjectView'
import SetupWizard from './components/SetupWizard'
import SettingsModal from './components/SettingsModal'
import GuidePanel from './components/GuidePanel'
import { HintsContext } from './hints'
import { getGuideSeen, getHintsOn, setGuideSeen, setHintsOn } from './prefs'

export default function App() {
  const [status, setStatus] = useState(undefined) // undefined = still loading
  const [pid, setPid] = useState(null)
  const [openChapter, setOpenChapter] = useState(null) // jump straight into the reader
  const [skipSetup, setSkipSetup] = useState(false)
  const [forceSetup, setForceSetup] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [showGuide, setShowGuide] = useState(false)
  const [hintsOn, setHintsOnState] = useState(() => getHintsOn())

  function openProject(id, chapter = null) { setPid(id); setOpenChapter(chapter) }
  function toggleHints(on) { setHintsOnState(on); setHintsOn(on) }
  function closeGuide() { setShowGuide(false); setGuideSeen() }
  function guideGo(target) {
    if (target === 'setup') setForceSetup(true)
    else if (target === 'settings') setShowSettings(true)
    else if (target === 'library') setPid(null)
  }

  useEffect(() => { api.status().then(setStatus).catch(() => setStatus({})) }, [])
  // Open the guide automatically the very first time (then never again unless asked).
  useEffect(() => { if (status !== undefined && !getGuideSeen()) setShowGuide(true) }, [status])

  if (status === undefined) {
    return <div className="flex min-h-screen items-center justify-center bg-page text-hint">Loading…</div>
  }

  const ready = status.config_present && status.claude_logged_in &&
    status.google_client_secret_present && status.google_logged_in

  return (
    <HintsContext.Provider value={{ on: hintsOn, setOn: toggleHints }}>
      {forceSetup || (!ready && !skipSetup) ? (
        <SetupWizard
          status={status}
          setStatus={setStatus}
          onDone={() => { setForceSetup(false); setSkipSetup(true) }}
        />
      ) : pid ? (
        <ProjectView
          pid={pid}
          status={status}
          initialChapter={openChapter}
          onBack={() => setPid(null)}
          onSettings={() => setShowSettings(true)}
          onGuide={() => setShowGuide(true)}
        />
      ) : (
        <ProjectLibrary
          status={status}
          onOpen={openProject}
          onSettings={() => setShowSettings(true)}
          onSetup={() => setForceSetup(true)}
          onGuide={() => setShowGuide(true)}
        />
      )}
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
      {showGuide && <GuidePanel onClose={closeGuide} onGo={guideGo} />}
    </HintsContext.Provider>
  )
}
