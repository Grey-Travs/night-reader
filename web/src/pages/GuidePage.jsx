import { useNavigate } from 'react-router-dom'
import GuidePanel from '../components/GuidePanel'

// The guide as a full-screen route. "Take me there" links jump to the matching page.
export default function GuidePage() {
  const navigate = useNavigate()
  const go = (target) => {
    if (target === 'settings') navigate('/settings')
    else navigate('/') // 'library' / 'setup' both land on the library
  }
  // Go back where they came from when there IS history, else fall back to the
  // library — so a hard-refresh / direct link / first-run landing on /guide (where
  // the overlay hides the sidebar) can never dead-end with no way out.
  const close = () => (window.history.state?.idx > 0 ? navigate(-1) : navigate('/'))
  return <GuidePanel onClose={close} onGo={go} />
}
