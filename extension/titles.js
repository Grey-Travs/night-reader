// The browser half of one answer to "are these two chapter titles the same one?".
//
// The twin of server/titles.py. Read that file first -- it carries the reasoning, and the
// reasoning is the part that must not drift. tests/test_title_parity.py runs both over the
// same cases and fails if they disagree.
//
// Loaded as an ordinary content script, listed BEFORE content.js in manifest.json so
// nrTitleKey exists by the time content.js runs. Same isolated world, injected in the
// order the manifest lists them.

/* eslint-disable no-unused-vars */

// Zero-width and invisible. Removed, not replaced with a space: they are zero WIDTH, so a
// word split by one reads as one word.
const NR_INVISIBLE = /[​‌‍﻿]/g

// Spelled out rather than \s. The two engines disagree about \s -- Python matches the
// separator controls and NEL, JavaScript matches the byte-order mark -- and that exact
// difference has already caused a silent bug in this project once.
const NR_SPACE = new RegExp(
  '[ \\t\\n\\r\\f\\v'
  + '\\u0085'          // NEL
  + '\\u00a0'          // no-break space
  + '\\u1680'          // ogham space mark
  + '\\u2000-\\u200a'  // en quad .. hair space
  + '\\u2028\\u2029'   // line / paragraph separator
  + '\\u202f'          // narrow no-break space
  + '\\u205f'          // medium mathematical space
  + '\\u3000'          // ideographic space
  + ']+', 'g')

// NFKC, drop the invisibles, collapse whitespace, trim, lowercase.
//
// toLowerCase rather than any attempt at casefolding: JS has no casefold, and a rule both
// languages implement identically beats a close approximation that drifts.
function nrTitleKey(title) {
  return String(title == null ? '' : title)
    .normalize('NFKC')
    .replace(NR_INVISIBLE, '')
    .replace(NR_SPACE, ' ')
    .trim()
    .toLowerCase()
}

// So the parity test can require() this from Node. Harmless in the browser, where there is
// no `module`.
if (typeof module !== 'undefined' && module.exports) module.exports = { nrTitleKey }
