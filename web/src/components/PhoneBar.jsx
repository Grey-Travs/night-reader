import { Link } from 'react-router-dom'

// The header of the phone's inner screens: the way back to the hub, which is the only
// way back from the installed app (it has no browser back button of its own on screen).
export default function PhoneBar({ title, back = '/phone', children = null }) {
  return (
    <header className="phone-bar">
      <Link to={back} className="btn btn-ghost shrink-0 px-2 py-2 text-sm">‹ Night Reader</Link>
      <span className="min-w-0 truncate text-sm font-medium">{title}</span>
      <span className="flex shrink-0 items-center gap-1">{children}</span>
    </header>
  )
}
