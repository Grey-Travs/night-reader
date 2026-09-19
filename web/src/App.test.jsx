import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'

// Every page is now fetched on demand, and nothing else in this suite renders App — the
// page tests import their component directly, so they resolve it eagerly and would pass
// even if lazy/Suspense were wired up wrongly. That is the exact shape of bug this repo
// has shipped before: a build that compiles and a route that renders nothing.
//
// So this asserts the two things only App can be wrong about: that a route actually
// arrives through its Suspense boundary, and that /phone escapes the two desktop
// behaviours that would otherwise take a phone somewhere it did not ask to go.

const h = vi.hoisted(() => ({ guideSeen: true }))

vi.mock('./prefs', async (actual) => ({
  ...(await actual()),
  getGuideSeen: () => h.guideSeen,
  setGuideSeen: () => {},
}))

// Every endpoint answers with one permissive shape. These tests are about routing, not
// about any page's data, and a page that renders its empty state has still proved that
// its chunk loaded and mounted.
vi.mock('./api', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      if (prop === 'backupAllUrl') return () => '#'
      if (prop === 'then') return undefined
      return vi.fn().mockResolvedValue({
        config_present: true,
        claude_logged_in: true,
        projects: [], series: [], jobs: [], results: [],
      })
    },
  }),
}))

function at(path) {
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>)
}

beforeEach(() => { h.guideSeen = true })

describe('routes arrive through their Suspense boundary', () => {
  it('loads the library behind the app shell', async () => {
    at('/')
    expect(await screen.findByText('Your library')).toBeInTheDocument()
  })

  it('loads the phone console', async () => {
    at('/phone')
    expect(await screen.findByText('Night Reader')).toBeInTheDocument()
  })
})

describe('/phone escapes the desktop', () => {
  it('brings none of the app shell with it', async () => {
    at('/phone')
    await screen.findByText('Night Reader')
    // The sidebar's own landmark. Its absence is the whole point of the route sitting
    // outside AppShell rather than being styled narrow.
    expect(screen.queryByText('Your library')).toBeNull()
    expect(screen.queryByRole('navigation')).toBeNull()
  })

  it('is not hijacked by the first-run guide redirect', async () => {
    // A phone opening the link has an empty localStorage, which is indistinguishable from
    // a first run — so without the guard the icon would land on the desktop guide.
    h.guideSeen = false
    at('/phone')
    expect(await screen.findByText('Night Reader')).toBeInTheDocument()
  })
})
