import { useEffect, useState } from 'react'
import { api } from './api'
import ProjectLibrary from './components/ProjectLibrary'
import ProjectView from './components/ProjectView'
import SetupWizard from './components/SetupWizard'
import SettingsModal from './components/SettingsModal'

export default function App() {
  const [status, setStatus] = useState(undefined) // undefined = still loading
  const [pid, setPid] = useState(null)
  const [openChapter, setOpenChapter] = useState(null) // jump straight into the reader
  const [skipSetup, setSkipSetup] = useState(false)
  const [forceSetup, setForceSetup] = useState(false)
  const [showSettings, setShowSettings] = useState(false)

  function openProject(id, chapter = null) { setPid(id); setOpenChapter(chapter) }

  useEffect(() => { api.status().then(setStatus).catch(() => setStatus({})) }, [])

  if (status === undefined) {
    return <div className="flex min-h-screen items-center justify-center bg-page text-hint">Loading…</div>
  }

  const ready = status.config_present && status.claude_logged_in &&
    status.google_client_secret_present && status.google_logged_in

  if (forceSetup || (!ready && !skipSetup)) {
    return (
      <SetupWizard
        status={status}
        setStatus={setStatus}
        onDone={() => { setForceSetup(false); setSkipSetup(true) }}
      />
    )
  }

  return (
    <>
      {pid ? (
        <ProjectView
          pid={pid}
          status={status}
          initialChapter={openChapter}
          onBack={() => setPid(null)}
          onSettings={() => setShowSettings(true)}
        />
      ) : (
        <ProjectLibrary
          status={status}
          onOpen={openProject}
          onSettings={() => setShowSettings(true)}
          onSetup={() => setForceSetup(true)}
        />
      )}
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </>
  )
}
