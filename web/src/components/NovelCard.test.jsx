import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import NovelCard from './NovelCard'

// The card is shared by the Library and the Archive, and it now carries the novel's
// status. Two things are worth pinning down.
//
// The first is that a series member's status is the SERIES' — a four-document novel shows
// four cards, and if each could be set separately they would drift apart and the shelf
// would contradict itself about one story. The server routes the write; the card has to
// say so, or changing it from part 2 looks like it did nothing to parts 1, 3 and 4.
//
// The second is that "ongoing" gets no pill. It is the default and it is what nearly
// every novel is, so badging it would put a label on every card and bury the two that
// actually mean something.

vi.mock('../prefs', () => ({ getLastRead: () => null }))
vi.mock('../format', () => ({ fmtCost: (n) => `$${n}` }))

const BASE = {
  id: 'p1',
  name: 'A Novel',
  translated: 10,
  chapter_count: 20,
  needs_review: 0,
  cost_usd: 0,
  status: 'ongoing',
  status_from: 'novel',
  series_id: null,
  series_name: '',
}

function card(over = {}, handlers = {}) {
  return render(
    <NovelCard
      p={{ ...BASE, ...over }}
      onOpen={() => {}}
      onToggleArchive={() => {}}
      onRemove={() => {}}
      {...handlers}
    />,
  )
}

describe('the status pill', () => {
  it('stays off an ongoing novel', () => {
    card()
    expect(screen.queryByText('Ongoing')).toBeNull()
  })

  it('shows for completed and hiatus', () => {
    card({ status: 'completed' })
    expect(screen.getByText('Completed')).toBeInTheDocument()
  })

  it('says which series a member takes its status from', () => {
    card({ status: 'hiatus', status_from: 'series', series_name: 'Big Series' })
    expect(screen.getByText('Hiatus')).toHaveAttribute(
      'title', 'Set for the whole of “Big Series”')
  })

  it('survives a novel saved before the field existed', () => {
    // Every one of the 58 novels on disk predates this, and none has a status key.
    const { status, ...without } = BASE
    render(<NovelCard p={without} onOpen={() => {}} onToggleArchive={() => {}} onRemove={() => {}} />)
    expect(screen.getByText('A Novel')).toBeInTheDocument()
    expect(screen.queryByText('Ongoing')).toBeNull()
  })
})

describe('setting the status', () => {
  it('offers all three and marks the current one', () => {
    card({ status: 'completed' }, { onSetStatus: vi.fn() })
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
    for (const label of ['Ongoing', 'Completed', 'Hiatus']) {
      expect(screen.getByRole('button', { name: new RegExp(`^${label}$`) })).toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: /^Completed$/ }))
      .toHaveAttribute('aria-current', 'true')
  })

  it('reports the pick', () => {
    const onSetStatus = vi.fn()
    card({}, { onSetStatus })
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
    fireEvent.click(screen.getByRole('button', { name: /^Hiatus$/ }))
    expect(onSetStatus).toHaveBeenCalledWith(expect.objectContaining({ id: 'p1' }), 'hiatus')
  })

  it('does not write the status it already has', () => {
    const onSetStatus = vi.fn()
    card({ status: 'hiatus' }, { onSetStatus })
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
    fireEvent.click(screen.getByRole('button', { name: /^Hiatus$/ }))
    expect(onSetStatus).not.toHaveBeenCalled()
  })

  it('names the series in the menu when that is what is being set', () => {
    card({ status_from: 'series', series_name: 'Big Series' }, { onSetStatus: vi.fn() })
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
    expect(screen.getByText('Status · all of “Big Series”')).toBeInTheDocument()
  })
})

describe('the rest of the menu still behaves', () => {
  it('has no overflow button at all when nothing would be in it', () => {
    // The Archive passes neither handler, and an empty menu is worse than no menu.
    card({ imported_from: { site: 'meiko' } })
    expect(screen.queryByRole('button', { name: /more actions/i })).toBeNull()
  })

  it('never offers re-translation for an imported novel', () => {
    // Its chapters are already English, so the queue would take zero and look broken.
    card({ imported_from: { site: 'meiko' } }, { onSetStatus: vi.fn(), onRetranslate: vi.fn() })
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
    expect(screen.getByText('imported')).toBeInTheDocument()
    expect(screen.queryByText(/re-translate/i)).toBeNull()
  })

  it('still offers re-translation for an ordinary novel', () => {
    card({}, { onRetranslate: vi.fn() })
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }))
    expect(screen.getByText(/re-translate whole novel/i)).toBeInTheDocument()
  })
})
