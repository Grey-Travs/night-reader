import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { useToast } from '../toast'
import { usePhoneOutbox } from '../outbox'
import { pagesNeedingYou, stepLabel, usePhoneProgress } from '../phoneProgress'
import PhonePage from './PhonePage'

// What the home-screen icon opens: the chapters sent from the phone first, then the
// posting console as it always was.
export default function PhoneHome() {
  return <PhonePage lead={<HandsFreeLead />} />
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
const clock = (seconds) => new Date(seconds * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

// How many recent chapters each novel shows.
const RECENT = 5

function HandsFreeLead() {
  const { novels, refresh } = usePhoneProgress()
  const [box, outbox] = usePhoneOutbox()
  const needs = pagesNeedingYou(novels)
  const shown = (novels || []).filter((novel) => (novel.chapters || []).length)
  const onPhone = outbox.chapters.filter((ch) => ch.state !== 'captured')
  return (
    <section aria-label="Chapters from the phone" className="space-y-3">
      <Link to="/phone/capture" className="btn btn-primary block w-full py-4 text-center text-base">
        Capture a chapter
      </Link>
      {!outbox.durable && (
        <p className="rounded-card px-3 py-2 text-xs pill-review">
          This browser isn’t keeping photos on the phone, so any not sent yet are lost if
          the app closes. Open Night Reader from its home-screen icon.
        </p>
      )}
      <Sending box={box} outbox={outbox} />
      {onPhone.map((ch) => <OnPhone key={ch.id} box={box} chapter={ch} />)}
      {needs.length > 0 && (
        <Link to="/phone/check" className="block rounded-card px-3 py-2 text-sm pill-review">
          {plural(needs.length, 'page')} need{needs.length === 1 ? 's' : ''} you →
        </Link>
      )}
      {shown.map((novel) => <NovelStatus key={novel.pid} novel={novel} onChange={refresh} />)}
    </section>
  )
}

function NovelStatus({ novel, onChange }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const rows = (novel.chapters || []).slice(-RECENT)

  async function act(call, done) {
    setBusy(true)
    try {
      await call()
      toast(done)
    } catch (e) {
      toast(e.message || 'The computer couldn’t be reached.')
    } finally {
      setBusy(false)
      onChange()
    }
  }

  return (
    <div className="card space-y-2 p-3">
      <h2 className="text-sm font-medium">{novel.name}</h2>
      {novel.stopped && (
        <Banner
          text="Hands-free is paused: chapters wait until you resume."
          action="Resume" busy={busy}
          onAct={() => act(() => api.resumeHandsFree(novel.pid), 'Carrying on')}
        />
      )}
      {!novel.stopped && novel.waiting && (
        <Banner
          text={`Waiting for your Claude plan${novel.waiting.resume_at ? ` — carries on at about ${clock(novel.waiting.resume_at)}` : ''}.`}
          action="Try now" busy={busy}
          onAct={() => act(() => api.resumeNow(novel.pid), 'Trying again now')}
        />
      )}
      {!novel.stopped && !novel.waiting && novel.gave_up && (
        <Banner
          text={`Stopped waiting for your Claude plan. Tries again at about ${clock(novel.gave_up.retry_at)}.`}
          action="Try now" busy={busy}
          onAct={() => act(() => api.resumeHandsFree(novel.pid), 'Trying again now')}
        />
      )}
      <ul className="space-y-1">
        {rows.map((row) => (
          <li key={row.batch} className="flex items-baseline justify-between gap-3 text-sm">
            <span className="shrink-0 font-medium">Chapter {row.number ?? '?'}</span>
            {row.step === 'needs-you' ? (
              <Link to="/phone/check" className="min-w-0 text-right text-xs text-ink underline">
                {stepLabel(row)}
              </Link>
            ) : (
              <span className="min-w-0 text-right text-xs text-hint">{stepLabel(row)}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

const time = (ms) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

// The photos still on this phone, and whether they can go.
function Sending({ box, outbox }) {
  const { status, unsent } = outbox
  if (status.state === 'offline') {
    return (
      <Banner
        text={`${plural(unsent, 'photo')} saved on this phone. The computer can’t be reached — trying again at ${time(status.retryAt)}.`}
        action="Try now" onAct={() => box?.retryNow()}
      />
    )
  }
  if (status.state === 'blocked') {
    return <Banner text={status.message} action="Try now" onAct={() => box?.retryNow()} />
  }
  if (unsent > 0) return <p className="text-xs text-hint">Sending {plural(unsent, 'photo')}…</p>
  return null
}

// A chapter still on the phone: being shot, being sent, or stuck with a way out.
function OnPhone({ box, chapter: ch }) {
  const continueAt = `/phone/capture?pid=${encodeURIComponent(ch.pid)}&shoot=${ch.id}`
  const number = ch.number ?? ch.shown
  const name = number ? `Chapter ${number}` : 'A new chapter'
  const photos = ch.photos.length
  const stuckAt = ch.stop?.photoId ? ch.photos.find((p) => p.id === ch.stop.photoId) : null
  if (ch.state === 'discarding') return <p className="text-xs text-hint">Removing {name}…</p>
  if (ch.state === 'retake') {
    return ch.stop ? (
      <Stuck text={`A photo taken again: ${ch.stop.message}`}>
        <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => box.retryChapter(ch.id)}>Try again</button>
        <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => box.discard(ch.id)}>Discard</button>
      </Stuck>
    ) : <p className="text-xs text-hint">Sending a photo taken again…</p>
  }
  if (ch.stop?.kind === 'box-gone') {
    return (
      <Stuck text={`${name} was deleted on the computer.`}>
        <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => box.resendAsNew(ch.id)}>
          Send as a new chapter
        </button>
        <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => box.discard(ch.id)}>Discard</button>
      </Stuck>
    )
  }
  if (ch.stop) {
    return (
      <Stuck text={`${name}${stuckAt ? `, photo ${stuckAt.order}` : ''}: ${ch.stop.message}`}>
        {stuckAt && (
          <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => box.skipPhoto(stuckAt.id)}>
            Skip that photo
          </button>
        )}
        <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => box.retryChapter(ch.id)}>Try again</button>
        <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => box.discard(ch.id)}>Discard</button>
      </Stuck>
    )
  }
  if (ch.state === 'open') {
    return (
      <div className="flex items-center justify-between gap-2 text-sm">
        <span>{name} · {plural(photos, 'photo')}, not finished</span>
        <span className="flex shrink-0 gap-1">
          <Link to={continueAt} className="btn btn-quiet px-2 py-1 text-xs">Continue</Link>
          <button className="btn btn-quiet px-2 py-1 text-xs" onClick={() => box.closeChapter(ch.id)}>
            Finish
          </button>
        </span>
      </div>
    )
  }
  return null
}

function Stuck({ text, children }) {
  return (
    <div className="space-y-2 rounded-card px-3 py-2 text-xs pill-review">
      <p>{text}</p>
      <div className="flex flex-wrap gap-2">{children}</div>
    </div>
  )
}

function Banner({ text, action, busy, onAct }) {
  return (
    <div className="flex items-center gap-2 rounded-card px-2 py-1.5 text-xs pill-review">
      <p className="flex-1">{text}</p>
      <button className="btn btn-quiet shrink-0 px-2 py-1 text-xs" disabled={busy} onClick={onAct}>
        {action}
      </button>
    </div>
  )
}
