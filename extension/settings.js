// Does the site agree with what we asked it to save?
//
// Its own module for the same reason as titles.js: this is the rule that decides whether a
// chapter is charging what it should, it is consulted from two places that MUST agree, and
// keeping it in content.js meant it could not be tested at all.
//
// The two callers:
//   * the check straight after a save -- stop the run rather than leave a chapter wrong;
//   * the reconcile that decides whether a half-posted chapter is already finished.
// When those disagree, a chapter that failed the save check reads as "finished" on the next
// run and is recorded as posted -- live at the wrong price, with nothing left to revisit it.

/**
 * What the site got wrong about a chapter's settings, as a list of readable phrases.
 * Empty means it agrees with us.
 *
 * Compared loosely on purpose: the server normalises what it is given, returning `paid`
 * as 1/0 where `true` was sent. Comparing strictly turned a correct save into a failure.
 *
 * A field the server did not send at all is NOT reported wrong. The save reply carries all
 * four, so this costs nothing there; the read-back the reconcile uses can omit `type`, and
 * treating "absent" as "wrong" would send every resumed chapter down the repair path and
 * re-save chapters that were already perfectly fine.
 */
function nrSettingsWrong(after, want) {
  const { state, coins } = want || {}
  const record = after || {}
  const has = (v) => v !== undefined && v !== null && v !== ''
  const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase()
  const wrong = []
  if (has(record.type) && !same(record.type, 'notes')) {
    wrong.push(`type is "${record.type}"`)
  }
  if (has(record.state) && !same(record.state, state)) {
    wrong.push(`state is "${record.state}"`)
  }
  if (has(record.coins) && Number(record.coins) !== Number(coins || 0)) {
    wrong.push(`coins is ${record.coins}`)
  }
  if (has(record.paid)) {
    const gotPaid = record.paid === true || Number(record.paid) === 1
    if (gotPaid !== (Number(coins) > 0)) wrong.push(`paid is ${record.paid}`)
  }
  return wrong
}

// So the tests can require() this from Node. Harmless in the browser, where there is no
// `module`.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { nrSettingsWrong }
}
