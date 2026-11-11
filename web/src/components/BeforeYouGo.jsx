// What Done shows while something about the chapter is still open: the photos worth
// taking again, each with Retake, how far reading has got, and anything still to send.
// Finishing anyway is always allowed — a page that needs you is pushed to the phone
// once you have left the camera.

function tallyLine({ read, checking, reading, total }) {
  const parts = [`${read} of ${total} read`]
  if (checking) parts.push(`${checking} being double-checked`)
  if (reading) parts.push(`${reading} still being read`)
  return `${parts.join(', ')}.`
}

export default function BeforeYouGo({ number, flagged, tally, onRetake, onFinish, onKeepShooting }) {
  return (
    <div className="camera-sheet" role="dialog" aria-label="Before you go">
      <p className="text-sm font-medium">Before you finish Chapter {number}</p>
      {flagged.length > 0 && (
        <ul className="space-y-2" aria-label="Photos to look at">
          {flagged.map(({ photo, badge }) => (
            <li key={photo.id} className="flex items-center gap-2 text-sm">
              <span className="flex-1">{badge.long}</span>
              <button className="camera-btn" onClick={() => onRetake(photo)}>Retake</button>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs">{tallyLine(tally)}</p>
      {tally.sending > 0 && (
        <p className="text-xs">{tally.sending} still sending — keep the app open a moment.</p>
      )}
      <div className="flex flex-wrap justify-center gap-2">
        <button className="camera-btn" onClick={onFinish}>{flagged.length ? 'Finish anyway' : 'Finish'}</button>
        <button className="camera-btn" onClick={onKeepShooting}>Keep shooting</button>
      </div>
      <p className="text-center text-xs opacity-80">
        If a page needs you after you leave, you’ll get a notification.
      </p>
    </div>
  )
}
