export const meta = {
  name: 'pages-chapter-boxes-implement',
  description: 'Implement ordered chapter boxes on the Pages tab (backend + frontend in parallel), adversarially review, then fix',
  phases: [
    { title: 'Implement', detail: 'backend (server/, tests/) and frontend (web/) implementers in parallel on disjoint files' },
    { title: 'Review', detail: 'contract, backend and frontend adversarial reviewers (read-only)' },
    { title: 'Fix', detail: 'backend and frontend fixers apply confirmed findings and re-run tests' },
  ],
}

const ROOT = 'C:/Users/Mommy/Documents/night-reader/night-reader'
const TASKS = 'C:/Users/Mommy/AppData/Local/Temp/claude/c--Users-Mommy-Documents-night-reader-night-reader/e566929a-06e3-4634-ba18-f02c9eb03d3f/tasks'
const SCRATCH = 'C:/Users/Mommy/AppData/Local/Temp/claude/c--Users-Mommy-Documents-night-reader-night-reader/e566929a-06e3-4634-ba18-f02c9eb03d3f/scratchpad'
const PLAN = 'C:/Users/Mommy/.claude/plans/by-the-way-in-gleaming-snail.md'

const SHARED = `
Repo: ${ROOT} (Windows; use the Bash tool with forward slashes, or PowerShell). Python: ${ROOT}/.venv/Scripts/python.exe (pytest is installed there). Web: ${ROOT}/web (npm).
Baseline before this work: \`.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider\` -> 1102 passed; \`npm test\` in web/ -> 181 passed; \`npm run build\` succeeds.

You are implementing an APPROVED plan. Read these first, in this order:
1. The approved plan: ${PLAN}
2. The detailed designs it was synthesized from: ${TASKS}/backend.md, ${TASKS}/frontend.md, and the adversarial critique ${TASKS}/critique.md (the plan already adopted the critique's R1-R17 fixes).
3. Research with file:line references if you need it: ${TASKS}/wnda12bmq.output (JSON; result.frontend/backend/ordering).
Where the designs disagree with each other, THE CONTRACT BELOW IS AUTHORITATIVE, then the plan, then critique.md, then backend.md/frontend.md.

HARD RULES:
- Never run git commands that change the working tree or history (no checkout, reset, stash, clean, commit, restore). Read-only git (status, diff, log, show) is fine. web/package-lock.json already has an intentional uncommitted change (npm audit fix): leave it alone.
- Put any throwaway scripts in ${SCRATCH}, never in the repo.
- Write code that reads like the surrounding code: this codebase uses narrative "why" comments that explain the failure a piece of code prevents (see existing comments in server/app.py, server/pages.py, web/src/pages/PagesPage.jsx). Match that density and voice; don't add comments that just restate code. Keep naming and idioms consistent with neighbours.
- Do not leave dead code behind: remove what the plan removes.

=== AUTHORITATIVE CONTRACT ===
pages.json v2 (server/pages.py):
- doc: {version: 2, rev: int, next_seq, pages: [...], batches: [...], build, totals}. load_pages: setdefaults (incl. rev=0), migrate_v1 if version < 2, then normalize (pure, deterministic, idempotent; reads never persist). save_pages: normalize, rev += 1, atomic write.
- box record (an entry of doc.batches; list order == box order): {id: 8 lowercase hex, label: str, at: iso str, count: int, chapter: int|None, built_sig: str|None} plus legacy: True only on a legacy box. Page records gain join_prev_id: str|None.
- normalize: as plan + backend.md section 1.5; orphan box ids are reused only if they match ^[a-f0-9]{8}$ and are free, otherwise derived deterministically (critique R15). Built+legacy boxes first in chapter order, unbuilt after (relative order kept); pages stable-sorted by box; counts recomputed; seams reconciled via join_prev_id against the previous NON-SKIPPED page in the same box (first page of a box has no seam).
- migrate_v1: as backend.md section 1.6 plus critique R16 (empty seams on pages in built boxes become join_prev "paragraph" with join_prev_source "migrated").

GET /api/projects/{pid}/pages -> {pages: [rows without text/raw_text, with chars and has_text (has_text uses page_has_text, which ignores invisible characters)], batches: [box rows], build, counts, totals, chapter_count: int, rev: int}
box row: {id, label, title, at, count, chapter: int|null, number: int|null, state, blockers: [{kind, count, page_ids, message}], legacy?: true, chapters?: [first, last] (legacy only)}
state is one of: empty | skipped (every photo skipped) | reading | unread | to-check | ready | built | changed | changed-waiting | legacy.
blocker kinds (in this order): empty, all-skipped, reading, unread, failed, no-text, to-check. Messages say what to do (critique R17), e.g. "IMG_5.jpg has no text. If it's a picture or a blank page, mark it Skip." / "IMG_7.jpg couldn't be read. Read it again, type its text, or Skip it."
number: built -> chapter; legacy -> null; unbuilt -> chapter_count + 1-based position among unbuilt boxes. chapter_count = max(build.chapters or 0, max box.chapter).

Upload: POST /api/projects/{pid}/pages?batch=&label=&name= (raw body, unchanged transport)
- batch "" -> a new box at the END (label via clean_title). Non-empty unknown batch -> 404 "That chapter box isn't in this novel any more." Legacy box -> 409.
- The page lands at the END of its box (not the end of the novel).
- success: {page, duplicate: false, batch, number, total, rev, pages}
- duplicate (checked first, as today): {page, duplicate: true, existing_batch, existing_number, total, rev, pages}. NO "batch" key on duplicates.
Structural writes return the fresh summary as "pages" and the post-save "rev", computed AFTER the mutate_pages block exits (upload currently returns from inside the block; restructure so rev is post-save):
- POST /pages/move {ids: [str], batch: str, before: str|null} -> {ok, changed: bool, rev, pages}
- POST /pages/batches {label?: str, after?: str|null, ids?: [str]} -> {batch: <box row>, rev, pages}. Creates the box right after "after" (default: at the end) and moves ids into it in the given order, atomically. "after" must be the last built box or an unbuilt box, else 409 (never clamp).
- POST /pages/batches/order {ids: [str]} -> {ok, rev, pages}. Full permutation of box ids (400 otherwise); the built/legacy prefix must be unchanged (409). DECLARE BEFORE /pages/batches/{batch_id}.
- POST /pages/batches/{batch_id} {label: str} -> {batch: <box row>, rev, pages}. Legacy -> 409.
- POST /pages/batches/{batch_id}/delete {expect_page_ids?: [str]|null} -> {ok, removed: int, rev, pages}. Built or legacy -> 409; expect_page_ids given and != the box's page ids (as a set) -> 409 "The photos in that chapter changed. Look again before deleting it." Unlinks image files outside the lock.
- POST /pages/delete {ids} -> {ok, removed, rev, pages}
- Box id path params not matching ^[a-f0-9]{8}$ -> 404.
- All NEW request models: model_config = ConfigDict(extra="forbid") (as server/app.py:215). BuildChapters too.
- REMOVE POST /pages/reorder and pages.reorder().
- POST /pages/{page_id} (update_page): returns the page record plus a top-level "rev" (post-save). Setting join_prev also sets join_prev_id to the page's current previous non-skipped neighbour in its box. A status change is followed by normalize (a skip/unskip resets the spanning seam) so the returned record reflects it.
- POST /pages/stitch {use_model, max_model_seams} -> {ok, seams, asked_model, gaps, remaining, cost_usd, usage, pages}. Pairs only inside one box, both pages with text, legacy boxes skipped, seams with join_prev_source "user" or "model" skipped. At write time (under the lock) skip a page whose neighbour or either text changed since the snapshot, or whose source became "user".
- POST /pages/build {include: "approved"|"all" = "approved", append: bool = true, force: bool = false, preview: bool = false, mode: str|null = null} (extra=forbid)
  -> {ok, preview: bool, chapters: int, added: [int], refreshed: [int], updated: [int], waiting: [{batch, number: int|null, title, built: bool, reasons: [str], blocked_by: int|null}], warnings: [str], unworked_seams: int, project?: <project summary, real builds only>}
  Errors: mode not in (null, "", "batch", "boxes") -> 400; include invalid -> 400; append and source.json chapter count != build.chapters -> 409 with a message naming Start over; a real build with nothing to add/refresh/update -> 400 ("No chapter is ready to build yet. Chapter N: <first reason>" or "Every chapter is already up to date."); start over (append=false) with any box that has photos but isn't ready -> 400 listing them (preview just reports them in waiting); start over that would renumber translated chapters without force -> 400 whose message still matches /already translated|renumber/i (keep today's wording).
  blocked_by: for a box waiting only because an earlier unbuilt box isn't ready, that earlier box's number; otherwise null.
  The whole real build runs inside mutate_pages (manifest read -> source.json read -> compute -> cache_source -> build record); project.json write, padding normalisation and consistency invalidation happen after the lock. build.page_map[str(N)] updated for every chapter written; used_page_ids kept (union of page_map values + legacy pages); "*" key dropped.
- _chapter_page_ids (server/app.py ~2192): filter out ids no longer in the manifest.

Frontend api.js (web/src/api.js), exact names:
- uploadPage(pid, file, {batch, label, name}) unchanged
- movePages(pid, {ids, batch, before = null})                -> POST /pages/move
- createPageBatch(pid, {label, after, ids} = {})              -> POST /pages/batches (omit undefined keys)
- reorderPageBatches(pid, ids)                                 -> POST /pages/batches/order {ids}
- renamePageBatch(pid, batchId, label)                         -> POST /pages/batches/{batchId} {label}
- deletePageBatch(pid, batchId, {expect_page_ids} = {})        -> POST /pages/batches/{batchId}/delete
- deletePages(pid, ids) unchanged; savePage unchanged; stitchPages unchanged
- buildChapters(pid, {include, append, force, preview})
- REMOVE reorderPages; delete web/src/reorder.js and web/src/reorder.test.js (carry their lessons into pageBoxes.test.js).
`

const BACKEND = `${SHARED}
YOUR JOB: implement the BACKEND half only: server/pages.py, server/ocr_build.py, server/app.py, and the tests in tests/ (test_pages_store.py, test_pages_api.py, test_ocr_build.py, plus anything else the change breaks). Do NOT edit anything under web/.
Follow the plan's "Backend design" and "Tests -> Backend" sections, using backend.md for detail and the contract above where they differ. Key points not to miss:
- normalize/migrate_v1 pure and deterministic; clean_title for box labels (safe_label stays for page names); page_has_text; BoxError mapped to HTTPException via a small context manager that wraps OUTSIDE mutate_pages so a refused mutation is never saved.
- ocr_build: remove usable_pages/_batch_runs/_batch_title/build_chapters/BUILD_MODES and the split import; add fill_seams, box_chapter, build_boxes (pure) with the refresh-in-place, one-photo-one-chapter, stop-at-first-unready and start-over rules; assemble_pages warns on a heading inside a box.
- Route order: every new literal route before GET/POST /pages/{page_id}; /pages/batches/order before /pages/batches/{batch_id}. Update the route-order note.
- Build holds the pages lock for the whole real build; test it deterministically by patching pj.cache_source to assert file_lock(pages_mod.pages_file(pid))._is_owned() (no threads).
- Delete tests whose premise is gone (reorder, heading/separator/single modes, contiguous-run grouping) and rewrite the rest as the plan lists. Keep test names descriptive like the existing ones.
Iterate until \`.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider\` passes fully (the whole suite, not just the pages tests).
Final answer: a concise report: files changed (with a one-line summary each), any deviation from the contract and why, the final pytest summary line, and anything you are unsure about.`

const FRONTEND = `${SHARED}
YOUR JOB: implement the FRONTEND half only, under web/ (web/src/...). Do NOT edit server/ or tests/. The backend is being implemented in parallel by another engineer against the same contract; your tests mock the api module, so you don't need a running server.
Follow the plan's "Frontend design" and "Tests -> Frontend" sections, using frontend.md for detail, with these overrides from the contract/critique:
- Build dialog uses "preview" (not dry_run) and the contract's build response shape (added/refreshed/updated are arrays of chapter numbers; waiting entries carry reasons and blocked_by). Build is enabled when added, refreshed or updated is non-empty. Toasts use added.length.
- Duplicates: read existing_batch / existing_number (null existing_number means a photo in the locked legacy box: word it accordingly). A duplicate never sets the drop's target box.
- Box rows carry blockers; build pill text from state + blockers (e.g. "2 to check", "1 not read", "All photos skipped"), not from re-counting page statuses.
- Write queue (critique R1): every rail-changing write (move, box create/order/rename/delete, page delete, savePage for text/status/seam incl. the autosave flush, acceptAllGood) goes through ONE FIFO promise chain with an optimistic update where sensible; apply a write's returned summary only if no further writes are queued; polls are ignored while writes are pending; any payload (poll, write, upload response) whose rev is lower than the highest rev already applied is dropped. Stitch stays outside the queue (its response is treated like a poll). Upload responses carry pages+rev: apply them through the same guarded apply so tiles appear as photos land (no ghost tiles needed; a per-job banner "Adding 3 of 10 to Chapter 5" is enough).
- Upload queue (critique R2/R3/R12): sortFilesForUpload (natural order only when all names share one pattern after replacing digit runs with '#', else keep picked order); checkDrop refuses the WHOLE drop before sending anything if any file is HEIC/HEIF, not JPEG/PNG/WebP, or over 25 MB (list the files; iPhone "Settings → Camera → Formats → Most Compatible" hint), skips empty files; on a mid-drop failure the queue PAUSES (later drops wait) with "Stopped at <name> (photo i of n)…" and [Try the rest again] (runs first) / [Cancel the rest]; jobs remember their pid; report duplicates elsewhere by chapter number.
- "Add chapter": if the last box is already empty, scroll to it instead of creating another; otherwise createPageBatch(pid, {}) and scroll to/focus the new box's + tile. File drops only on explicit targets (a box, the New chapter zone, the Add chapter button, the empty state); window-level preventDefault for missed file drops plus a toast.
- Built pills: use the outlet context's \`chapters\` rows (find by index === box.chapter): show the translation status; when source_changed is true show "Changed since translated — translate again".
- Photo view via ?photo=<id> (useSearchParams/useNavigate). The existing PagesPage.test.jsx mocks react-router-dom with only useOutletContext: switch to a partial mock (importOriginal) + MemoryRouter, as App.test.jsx does.
- Preserve every existing editor behaviour listed in the plan; fix the test mock name uploadPages -> uploadPage; add no-op scrollIntoView/scrollTo to web/src/test/setup.js; update web/src/guide.js (lines ~40-54) to describe chapter boxes.
- Components: web/src/pageBoxes.js (+ pageBoxes.test.js), web/src/components/ChapterBoxes.jsx, web/src/components/PagePhotoView.jsx, web/src/components/BuildChaptersDialog.jsx; PagesPage.jsx becomes the container.
- Use the codebase's token classes (card, pill-*, btn btn-primary/btn-quiet/btn-ghost, rounded-card, border-line, text-hint, sunken, rowhover, tap) and shared Modal/useConfirm/useToast/showError; page work (read/verify) only via submitTask. Touch: every drag action also has a non-drag control.
Iterate until \`npm test\` passes fully and \`npm run build\` succeeds (run them in ${ROOT}/web).
Final answer: a concise report: files added/changed/deleted (one line each), any deviation from the contract and why, final npm test summary and build result, and anything you are unsure about.`

phase('Implement')
const [backendReport, frontendReport] = await parallel([
  () => agent(BACKEND, { label: 'implement backend', phase: 'Implement' }),
  () => agent(FRONTEND, { label: 'implement frontend', phase: 'Implement' }),
])
log('Implementation done; starting adversarial review')

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['P0', 'P1', 'P2'] },
          side: { type: 'string', enum: ['backend', 'frontend', 'both'] },
          file: { type: 'string' },
          line: { type: 'number' },
          problem: { type: 'string' },
          evidence: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['severity', 'side', 'file', 'problem', 'evidence', 'fix'],
      },
    },
    tests: { type: 'string' },
  },
  required: ['findings', 'tests'],
}

const REVIEW_BASE = `${SHARED}
The implementation is now done by two engineers. Their reports:
=== BACKEND REPORT ===
${backendReport || '(no report)'}
=== FRONTEND REPORT ===
${frontendReport || '(no report)'}

You are a READ-ONLY adversarial reviewer: do NOT edit repo files (throwaway probe scripts go in ${SCRATCH}). Use \`git diff\` and \`git status\` to see the change (new files are untracked: read them directly). Default to skepticism: assume there are bugs and find them, but only report findings you have EVIDENCE for (quote code, or a probe/test run). Severity: P0 = can mix up story order / lose or duplicate text / data loss / crash / contract mismatch that breaks a feature; P1 = wrong behaviour the user would hit, missing plan item, misleading message, untested critical path; P2 = minor polish. Also report P1 for any plan item that is missing or any test that doesn't actually test what its name claims.`

phase('Review')
const reviews = await parallel([
  () => agent(`${REVIEW_BASE}
YOUR FOCUS: CONTRACT CONSISTENCY between web/ and server/. For every api.js method and every call site in web/src that uses it, compare against the server route, request model and response shape: names, field names, types, status codes, error-message regexes the UI matches (e.g. /already translated|renumber/i), what the UI reads from responses (pages, rev, batch, existing_batch, existing_number, added/refreshed/updated arrays, waiting[].reasons/blocked_by, unworked_seams, remaining), box row fields and state values the UI switches on, and extra="forbid" models vs keys the client actually sends (an unexpected key = 422!). Also confirm route declaration order in server/app.py (every new literal route before /pages/{page_id}; /pages/batches/order before /pages/batches/{batch_id}). Run both suites (\`.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider\` and \`npm test\` in web/) and report their summary lines in "tests".`,
    { label: 'review: contract', phase: 'Review', schema: FINDINGS }),
  () => agent(`${REVIEW_BASE}
YOUR FOCUS: the BACKEND (server/pages.py, server/ocr_build.py, server/app.py, tests/). Check against the plan and the 17 ordering risks in the research (result.ordering). Hunt for: normalize not idempotent/deterministic, seam reconciliation wrong (skipped pages, first page, moves to same spot), migrate_v1 errors, box_signature missing an input, blockers/state/number wrong, move_pages anchor edge cases, create_box "after" rule, reorder_boxes prefix rule, delete with expect_page_ids, upload landing position / unknown box / duplicate shape, rev not post-save, BoxError saved by accident (context manager order), build_boxes rules (refresh in place, one photo one chapter fixpoint, stop at first unready, blocked_by, start over removing empty boxes and refusing unready, legacy), page_map/used_page_ids bookkeeping, 409 chapter-count guard, lock held during build, stitch scoping and write-time validation and "remaining", _chapter_page_ids filtering, OCR worker interactions (it finds pages by seq; result writes must not break grouping/seams). Write and run small probe scripts in ${SCRATCH} using the real functions (patch PROJECTS_DIR to a temp dir as tests do) to confirm suspected bugs. Run the full pytest suite and report its summary in "tests".`,
    { label: 'review: backend', phase: 'Review', schema: FINDINGS }),
  () => agent(`${REVIEW_BASE}
YOUR FOCUS: the FRONTEND (web/src). Check against the plan's Frontend design and Behaviour sections. Hunt for: write-queue/rev-guard holes (a stale poll or out-of-order response undoing a move; optimistic update not rolled back on error; queue stuck after a rejected promise), upload queue (sorting rule both branches, checkDrop refusing the whole drop, duplicate never retargeting, pause on failure + retry-first + cancel, pid captured, banner), drag-and-drop (custom MIME + setData, dragend cleanup, file vs tile drags, drops only on explicit targets, window preventDefault), photo view (?photo= param, Back, arrow keys guarded, autosave FLUSHED on step/back/unmount, read-only while reading + refetch when done, seam control, Earlier/Later/Move to chapter/Split), build dialog (preview on open and on include toggle, waiting reasons, blocked_by, all warnings, unworked_seams hint, start-over double confirm with useConfirm, stable onClose), box header (rename, move up/down only in the unbuilt tail, delete with expect_page_ids, collapsed built boxes, built pill from chapters/source_changed), "Add chapter" scroll-to-empty rule, accessibility (keyboard/touch alternatives to drag), and whether the tests genuinely exercise these behaviours (no tests that pass vacuously). Run \`npm test\` and \`npm run build\` in web/ and report summaries in "tests".`,
    { label: 'review: frontend', phase: 'Review', schema: FINDINGS }),
])

const all = reviews.filter(Boolean).flatMap((r) => r.findings || [])
const testNotes = reviews.filter(Boolean).map((r) => r.tests).join('\n---\n')
const forSide = (side) => all.filter((f) => f.side === side || f.side === 'both')
log(`Review found ${all.length} findings: ${all.filter((f) => f.severity === 'P0').length} P0, ${all.filter((f) => f.severity === 'P1').length} P1, ${all.filter((f) => f.severity === 'P2').length} P2`)

const fixPrompt = (side, scope) => `${SHARED}
Reviewers checked the implementation. Here are the findings for the ${side.toUpperCase()} (JSON):
${JSON.stringify(forSide(side), null, 2)}

Reviewer test notes:
${testNotes}

YOUR JOB: you are the ${side} engineer; you may edit only ${scope}. For EACH finding: verify it against the code first (reviewers can be wrong). If it's real: fix it (P0 and P1 always; P2 if small and clearly right), and add or adjust a test that would have caught it when it is a behaviour bug. If it's not real, say why. If a finding needs a change on the other side of the contract, do not edit outside your scope: describe exactly what the other side must change in your report.
Then run your full suite until green (${side === 'backend' ? '`.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider`' : '`npm test` and `npm run build` in web/'}).
Final answer: for each finding: fixed / not a bug (why) / left (why); then the final test summary line(s); then any cross-side change needed.`

phase('Fix')
const [backendFix, frontendFix] = await parallel([
  () => forSide('backend').length
    ? agent(fixPrompt('backend', 'server/ and tests/'), { label: 'fix backend', phase: 'Fix' })
    : Promise.resolve('No backend findings.'),
  () => forSide('frontend').length
    ? agent(fixPrompt('frontend', 'web/'), { label: 'fix frontend', phase: 'Fix' })
    : Promise.resolve('No frontend findings.'),
])

return { backendReport, frontendReport, findings: all, testNotes, backendFix, frontendFix }
