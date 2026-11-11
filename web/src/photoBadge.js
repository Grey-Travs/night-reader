import { FLAG_TEXT, flagLine } from './photoCheck'

// What a photo's badge says while its chapter is being shot, from what is known so far:
// the phone's own check at the shutter, Claude Haiku's quick look a few seconds later,
// then Claude's full read and, when it isn't sure, its double-check.
//
// Each badge has a short form for the thumbnail and a long one for the list of flagged
// photos, and `flag` says whether it is a reason to take the photo again. The computer's
// later word wins over the phone's guess: a photo the phone thought blurry that Claude
// read cleanly is fine.
//
//   tone: 'quiet' — nothing to see yet; 'good' — read; 'warn' — worth a look; 'bad' — needs you

export function photoBadge(photo, row = null) {
  const flags = photo.check?.flags || []
  const order = photo.order
  if (photo.state === 'elsewhere') {
    const where = photo.elsewhere?.number ? ` in chapter ${photo.elsewhere.number}` : ' elsewhere'
    return { tone: 'warn', short: 'Already in', long: `Photo ${order} is already${where}.`, flag: false }
  }
  if (photo.state === 'skipped') return { tone: 'quiet', short: 'Skipped', long: `Photo ${order} was skipped.`, flag: false }
  if (photo.state !== 'sent' || !row) {
    if (flags.length) {
      return { tone: 'warn', short: FLAG_TEXT[flags[0]], long: `${flagLine(flags, order)}.`, flag: true }
    }
    return photo.state === 'sent'
      ? { tone: 'quiet', short: 'Sent', long: `Photo ${order} is on the computer.`, flag: false }
      : { tone: 'quiet', short: 'Saved', long: `Photo ${order} is saved on the phone, waiting to send.`, flag: false }
  }
  const look = row.look?.verdict === 'retake' ? row.look : null
  const doubt = look ? `Hard to read${look.note ? `: ${look.note}` : ''}` : ''
  switch (row.step) {
    case 'failed':
      return { tone: 'bad', short: 'Couldn’t read', long: `Photo ${order}: ${row.note || 'Claude couldn’t read it.'}`, flag: true }
    case 'needs-you':
      return { tone: 'bad', short: 'Needs you', long: `Photo ${order}: ${row.note || 'needs you.'}`, flag: true }
    case 'double-checking':
      return { tone: 'warn', short: 'Checking…', long: `Photo ${order}: Claude isn’t sure — double-checking…`, flag: true }
    case 'reading':
      if (look) return { tone: 'warn', short: 'Hard to read', long: `Photo ${order}: ${doubt}.`, flag: true }
      if (flags.length && row.looking) {
        return { tone: 'warn', short: FLAG_TEXT[flags[0]], long: `${flagLine(flags, order)}.`, flag: true }
      }
      return { tone: 'quiet', short: 'Reading…', long: `Photo ${order} is being read.`, flag: false }
    case 'skipped':
      return { tone: 'quiet', short: 'Skipped', long: `Photo ${order} was skipped.`, flag: false }
    default:
      return { tone: 'good', short: '✓', long: `Photo ${order} was read.`, flag: false }
  }
}

/** How the chapter is getting on, in one line, for "Before you go". */
export function chapterTally(photos, rows) {
  let read = 0
  let checking = 0
  let sending = 0
  let reading = 0
  for (const photo of photos) {
    if (photo.state === 'waiting') { sending += 1; continue }
    const row = rows.get(photo.pageId)
    if (!row) { reading += 1; continue }
    if (row.step === 'read') read += 1
    else if (row.step === 'double-checking') checking += 1
    else if (row.step === 'reading') reading += 1
  }
  return { read, checking, sending, reading, total: photos.length }
}
