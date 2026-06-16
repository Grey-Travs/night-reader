// All the guide text lives here as plain data, so it's easy to edit in one place.
// Written for a complete beginner: short sentences, no jargon, one idea per line.

export const QUICK_START = [
  'Add your novel — paste a Google Docs link, or paste/upload the text.',
  'Press “Translate all remaining” — the app turns the Korean chapters into English.',
  'Click a chapter to read it. When you’re done, Export it to your e-reader.',
]

// Each step: an instruction + a “How it works” reveal. `flow` draws a little
// boxes-and-arrows picture of the process. `go` jumps you to that screen.
export const STEPS = [
  {
    icon: '🔌',
    title: 'Get set up (one time)',
    instruction: 'Connect the two things the app needs: Google (to read your document) and Claude (to do the translating).',
    how: [
      'Look at the top-right of the home page. Two little dots — “Google” and “Claude” — turn green when they’re connected.',
      'If a dot is grey, click “Setup” and follow the steps.',
      'You only do this once on this computer.',
    ],
    go: 'setup',
    goLabel: 'Open Setup',
  },
  {
    icon: '📚',
    title: 'Add a novel',
    instruction: 'Add a book in one of two ways: paste a Google Docs link, or paste the text / load a .txt file.',
    flow: ['Your doc or text', 'Add a novel', 'It appears in your library'],
    how: [
      'On the home page, use the “Add a novel” box.',
      '“Google Doc”: paste the share link. The doc should have one chapter per tab.',
      '“Paste / .txt”: give it a name, paste the text (or click “Load .txt”), and choose how chapters are split.',
    ],
    go: 'library',
    goLabel: 'Go to the home page',
  },
  {
    icon: '🌐',
    title: 'Translate the chapters',
    instruction: 'Open a novel and press “Translate all remaining”, or tick only the chapters you want. They line up and translate one after another.',
    flow: ['Korean chapter', 'Translate', 'Claude writes English', 'Quality check', 'You can read it'],
    how: [
      'Click a novel to open it, then click the big “Translate all remaining” button.',
      'Or tick the boxes next to a few chapters and click “Translate selected”.',
      'A “queue” shows what’s translating now and what’s waiting. You can keep adding more while it runs.',
      'This uses your Claude plan. If the plan hits its limit it pauses and picks back up later.',
    ],
  },
  {
    icon: '🏷️',
    title: 'Keep names spelled the same (Glossary)',
    instruction: 'The glossary makes sure a character or place is spelled the same way in every chapter — so “Cassian” never becomes “Kassian”.',
    how: [
      'Open a novel and click “Glossary” at the top.',
      'New names the app finds appear under “New terms to review” — approve or fix them.',
      'If some chapters are already in English, click “Learn names” to copy those spellings in automatically.',
      'A name showing “— EN” is an English spelling to match (its Korean isn’t known yet — that fills in as you translate).',
    ],
  },
  {
    icon: '📖',
    title: 'Read your novel',
    instruction: 'Click any chapter title to read it. You can show the Korean original, change the text size, copy it, or download it.',
    how: [
      'Use “← Prev” / “Next →” (or the arrow keys) to move between chapters.',
      '“Show original” puts the Korean next to the English.',
      '“Aa” changes text size, width, and a softer “sepia” background.',
      '“Copy text” copies just the chapter (tips and buttons are never copied).',
    ],
  },
  {
    icon: '✅',
    title: 'Check the quality',
    instruction: 'Open a chapter and press “Check chapter” to scan it for problems. If something’s found, a pop-up lets you fix it.',
    flow: ['Open chapter', 'Check chapter', 'See any problems', 'Fix automatically'],
    how: [
      '“Check chapter” is fast and free — it looks for leftover notes, untranslated bits, and length problems.',
      '“Deep check with AI” has Claude read the whole chapter to catch anything the quick check misses (this uses your plan).',
      '“Fix automatically” removes stray text in place. The original is backed up first.',
      'You can also turn on automatic checking in Settings, so every new chapter is checked for you.',
    ],
  },
  {
    icon: '💾',
    title: 'Save / Export',
    instruction: 'When chapters are translated, download the whole novel as an EPUB (for e-readers), Markdown, or plain text.',
    how: [
      'Open a novel and click “Export” at the top.',
      'EPUB is best for a Kindle/Kobo/phone reader — it has a table of contents.',
      'Reading, copying, and exporting are free — they don’t use your Claude plan.',
    ],
  },
]

// What the coloured tags and dots mean.
export const LEGEND = [
  { label: 'Translated', cls: 'pill-translated', meaning: 'Done and quality-checked. Ready to read.' },
  { label: 'Needs review', cls: 'pill-review', meaning: 'The app wasn’t fully sure. Open it and press “Check chapter” to see why.' },
  { label: 'Already English', cls: 'pill-english', meaning: 'That chapter was already in English, so it was left as-is.' },
  { label: 'Queued', cls: 'pill-queued', meaning: 'Waiting in line to be translated.' },
  { label: 'Translating', cls: 'pill-translating', meaning: 'Being translated right now.' },
]

// Plain-language meanings for the words you’ll see.
export const TERMS = [
  ['Glossary', 'A name list that keeps spellings the same across the whole book.'],
  ['Canonical name', 'An English spelling to match (its Korean isn’t known yet).'],
  ['Validation', 'Automatic quality checks that run right after a chapter is translated.'],
  ['Deep check', 'Claude re-reads a finished chapter to catch leftover notes or mistakes.'],
  ['Needs review', 'A chapter the app flagged for you to look at.'],
  ['Queue', 'The line of chapters waiting to be translated.'],
]

export const FAQ = [
  ['It says “method not allowed”, or a new button doesn’t work',
   'Close the app window and run start.bat again, then refresh the page (Ctrl+Shift+R). New features need the app restarted.'],
  ['Translation “paused”',
   'Your Claude plan reached its limit for now. Your progress is saved — it resumes automatically later, or click “Resume now”.'],
  ['A “Google not connected” message',
   'Click “Setup” at the top and connect Google (read-only access to your own documents).'],
  ['I see odd AI notes inside a chapter',
   'Open that chapter and press “Check chapter” → “Fix automatically”. Turn on auto-checking in Settings to prevent it.'],
]

export const COST_NOTE =
  'These use your Claude plan: Translate, Learn names, and Deep check. Reading, copying, searching, and exporting are free.'
