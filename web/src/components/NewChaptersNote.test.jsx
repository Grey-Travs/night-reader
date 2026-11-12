import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import NewChaptersNote, { chapterRuns } from './NewChaptersNote'

// The notice the Series and Posting pages show when chapters added to a document were
// numbered on their own, or could not be.

describe('chapterRuns', () => {
  it('folds a run into a range and keeps breaks', () => {
    expect(chapterRuns([217, 218, 219, 235])).toBe('217–219, 235')
    expect(chapterRuns([5])).toBe('5')
  })
})

describe('new chapters note', () => {
  it('shows nothing when nothing changed', () => {
    const { container } = render(<NewChaptersNote added={[]} waiting={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('says which chapters were numbered on their own', () => {
    render(<NewChaptersNote added={Array.from({ length: 19 }, (_, i) => 217 + i)} />)
    expect(screen.getByRole('status').textContent).toMatch(/Numbered 19 new chapters on its own: ch 217–235\./)
  })

  it('names the document whose new chapters need a look, with the way out', () => {
    render(
      <NewChaptersNote
        waiting={[{ project_id: 'p3', name: 'Becoming 3', count: 2 }]}
        action={<button>Re-read documents</button>}
      />,
    )
    expect(screen.getByRole('status').textContent).toMatch(/2 new chapters in Becoming 3 need a look/)
    expect(screen.getByRole('button', { name: 'Re-read documents' })).toBeTruthy()
  })
})
