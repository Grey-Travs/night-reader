# Pages tab: ordered chapter boxes

## Short answer
Yes, chapter boxes are the better approach. The app already half-does it: every upload drop
is saved as a hidden "batch", and Build can turn each batch into one chapter. The screen never
shows batches, though. The only hint is a sentence in the guide: "Photos you add together
count as one chapter. Add the next chapter's photos as a separate batch"
(`web/src/guide.js:46`). This plan makes the boxes visible, as in the sketch. It also fixes
the ways story order can break today, none of which warn you.

User decisions (asked):
- The overview shows only chapter boxes. Clicking a photo opens a full-width photo + text
  view, with ← → stepping within that chapter and a "Back to chapters" link.
- Boxes only: each box builds exactly one chapter, in box order. The heading, separator and
  single build modes are removed.

## Context: how order breaks today (verified by research)
1. A photo still "needs-check" (or empty) at Build is silently left out of the middle of its
   chapter. Once approved, it comes back later as its own chapter at the end
   (`server/ocr_build.py:35-38`, `server/app.py:1686,1727-1730`).
2. One failed photo stops the upload. Re-dropping puts the rest into a new batch, so one
   chapter becomes two.
3. Two drops at once interleave page by page.
4. Files upload in whatever order the browser gives them. Nothing sorts them, so IMG_10 can
   land before IMG_9.
5. A built chapter is frozen to its build-time photo list. Reordering does nothing, and a
   photo added to it becomes a new chapter at the end.
6. Build warnings ("a page may be missing…") are returned but never shown.
7. Dragging a photo into another batch's region splits chapters apart. There is no way to
   move a photo between chapters.
8. Seams go stale after reorder or delete. Stitch zips across chapter boundaries and re-asks
   the same first 25 seams on every run.
9. Drag state leaks: a cancelled drag later moves a page. A stale poll can undo a drag.
10. The reader's Photo view keeps build-time photo order and shows deleted photos.

## What the user will see
- **The Pages tab is a list of chapter boxes in reading order.** Each box shows:
  - "Chapter N", plus an optional title (the title becomes the chapter title).
  - A status pill: Not read / Reading… / 2 to check / Ready to build /
    Built · <translation status> / Changed — build to update / Changed since translated —
    translate again.
  - Photo tiles numbered 1, 2, 3…, with join markers between them, and a dashed "+" tile.
- **Adding photos.**
  - "Add chapter" adds an empty box at the end. If the last box is already empty, it scrolls
    there instead.
  - "+", or dropping photos on a box, adds them to the end of that box.
  - Dropping photos on the "New chapter" zone below the last box starts a new box with them.
  - Photos go in by file name (IMG_9 before IMG_10) when the names follow one pattern;
    otherwise they keep the order they were picked in.
  - The whole drop is checked first, so an iPhone HEIC photo, a PDF or an oversized file is
    refused before anything is sent.
- **Fixing order.** On a computer, drag a photo. On a phone or computer, open the photo and
  use Earlier / Later / Move to chapter… / Split chapter here.
- **Build.** Each box becomes one chapter, in box order.
  - A box with an unread or still-to-check photo waits. It is never built without that photo.
  - Boxes after a waiting box wait too, so numbering stays right.
  - Built chapters keep their place, but follow later fixes on the next Build: reordering,
    adding or moving photos, and text edits.
  - The Build dialog previews what will happen, says why a box waits, and shows every
    warning.

## Backend design

### Storage: pages.json version 2 (`server/pages.py`)
- **`batches`** are the chapter boxes, in box order:
  `{id (8 hex), label, at, count, chapter: int|null, built_sig: str|null, legacy?: true}`.
- **`pages`** stays one list, always grouped by box in box order. That means the OCR queue,
  stitch and build all follow box order. Each page gains `join_prev_id`: the page its seam
  was decided against.
- **`rev`** is a counter bumped on every save. It lets the client drop stale responses.
- **`normalize(doc)`** is pure and idempotent. It runs in `load_pages` and again in
  `save_pages` before writing. It:
  1. Drops bad records.
  2. Adopts orphan pages into a box with a derived 8-hex id.
  3. Removes empty legacy boxes.
  4. Puts built and legacy boxes first (in chapter order), then unbuilt boxes.
  5. Stable-sorts pages by box, then recounts.
  6. Clears any seam whose `join_prev_id` is not the page's current previous non-skipped
     page in the same box, even a seam the user set. This covers move, delete, skip/unskip
     and box delete in one place. The first page of a box has no seam.
- **`migrate_v1(doc)`** runs when `version < 2`. It is deterministic, and the next save
  persists it. No real v1 data exists on this machine, so this is for correctness only.
  - Each contiguous run of same-batch pages becomes its own box, in first-appearance order.
  - Empty v1 batches are dropped.
  - Pages in a numeric v1 `page_map` go to a box with `chapter=K`. A page that missed its v1
    build rejoins its chapter's box.
  - Pages from a v1 whole-novel split (`"*"`) go into one locked legacy box.
  - Empty seams in built boxes become `paragraph` (source "migrated"), so untouched
    translated chapters don't show as changed.
- **Helpers:**
  - `clean_title`: keeps `: ! ? "` (unlike `safe_label`, which stays for file names only),
    strips control characters, caps at 120.
  - `page_has_text`: ignores invisible characters.
  - `box_numbers`: a built box uses its `chapter`; an unbuilt box gets
    `chapter_count + its position among unbuilt boxes`.
  - `box_title`: label → first page's heading → "Chapter N". Moved from
    `ocr_build._batch_title`.
  - `box_signature`: label, plus the live pages' ids, texts, joins/glue and headings.
  - `box_blockers`: empty, all-skipped, reading, unread, failed, no-text, to-check. Each
    names its pages and gives an actionable message, e.g. "IMG_5 has no text — if it's a
    picture or blank page, mark it Skip".
  - `box_state`.
- **Mutations** raise `BoxError(status, message)`, which app.py maps to HTTP:
  - `create_box(label, after, ids)`: creates the box and moves the photos atomically. An
    `after` that would put it before a built box → 409.
  - `rename_box`: legacy → 409.
  - `reorder_boxes`: takes a full permutation; the built prefix is fixed (409).
  - `delete_box(expect_page_ids)`: built → 409; photo-list mismatch → 409.
  - `add_page`: unknown box → 404, legacy → 409. The page lands at the end of its box.
  - `move_pages(ids, box, before)`: any unknown id → 404 and nothing moves; legacy → 409;
    moving to the same spot is a no-op with no seam reset.
  - `reorder()` is removed.
- **`summary()`:** each box row is
  `{id, label, title, count, chapter, number, state, blockers, legacy?, chapters?:[first,last]}`.
  The summary also carries `chapter_count` and `rev`.

### Endpoints (`server/app.py`)
- **Rules for every endpoint below.**
  - Every literal route is declared before `/pages/{page_id}`.
  - New request models use `ConfigDict(extra="forbid")`, as at `app.py:215`.
  - Structural writes return the fresh summary as `pages`, built after the lock exits so its
    `rev` is the post-save value. The structural writes are upload, move, the box routes
    and `/pages/delete`.
- **`POST /pages?batch=&name=` (upload):**
  - The photo goes to the end of that box.
  - `batch=""` creates a new box at the end.
  - An unknown box → 404, never a silent new box.
  - A duplicate returns `{duplicate:true, existing_batch, existing_number}` with no `batch`
    key, so a stale cached client can't redirect a drop.
- **New routes:**
  - `POST /pages/move {ids, batch, before}`
  - `POST /pages/batches {label?, after?, ids?}`
  - `POST /pages/batches/order {ids}`, declared before `/batches/{id}`
  - `POST /pages/batches/{id} {label}`
  - `POST /pages/batches/{id}/delete {expect_page_ids?}`
- **Removed:** `POST /pages/reorder` (`app.py:1439-1449`). On the client, `api.reorderPages`,
  `web/src/reorder.js` and `reorder.test.js` go too.
- **`POST /pages/{page_id}` (update):**
  - Setting `join_prev` also records `join_prev_id`.
  - A status change runs `normalize`, so a skip resets the seam that spans it.
  - The response includes `rev`.
- **Stitch:**
  - Only pairs inside one box, where both pages have text. Legacy boxes are skipped.
  - Seams decided by the user or the model are skipped, so repeated presses get past the
    25-seam cap.
  - At write time it skips a page whose neighbour or text changed, or that the user set,
    during the model call.
  - The response adds `remaining`.
- **Build: `POST /pages/build {include: approved|all, append=true, force=false, preview=false, mode?}`**
  - Validation:
    - A `mode` other than none/`batch`/`boxes` → 400.
    - The whole build runs inside `mutate_pages`, so it is serialised with uploads, moves
      and OCR writes. That closes today's race, where the build reads the manifest unlocked.
    - Appending when the source.json chapter count ≠ `build.chapters` → 409, naming Start
      over.
  - `ocr_build.build_boxes` is pure and works in three steps:
    1. **Rewrite changed built boxes in place** from their current pages and current order.
       - A blocked box waits.
       - A box also waits if one of its photos still belongs to another chapter this build
         can't rewrite, so one photo never feeds two chapters.
       - If the paragraphs changed, the chapter is listed in `refreshed`, with a
         re-translate warning. Otherwise it goes in `updated` (title or photo list only).
    2. **Append unbuilt boxes in order.** The build stops at the first box that isn't ready.
       Every later box waits with "comes after chapter N".
    3. **Fill undecided seams** with the free deterministic `propose_join` rules, but only
       when they are confident. Unsure seams become paragraphs, with one warning per
       chapter. Filled seams are written back before hashing.
  - The response is the same for a real build and a preview:
    `{chapters, added:[int], refreshed:[int], updated:[int], waiting:[{batch, number, title, built, reasons, blocked_by}], warnings, unworked_seams, project?, preview}`.
  - When there is nothing to do it returns 400, with the first waiting reason or "Every
    chapter is already up to date".
  - Bookkeeping:
    - `build.page_map[str(N)]` is updated for every chapter written, so the reader's photo
      strip follows.
    - `used_page_ids` is kept for compatibility.
    - The `"*"` key is dropped.
  - **Start over** (`append=false`):
    - Removes empty boxes, built ones included.
    - Refuses with 400, listing the boxes with photos that aren't ready.
    - Keeps the existing force check for translated chapters.
    - A legacy box becomes one chapter, and the confirm says so.
- **`server/ocr_build.py` cleanup:**
  - Remove `usable_pages` (the silent drop), `_batch_runs`, `_batch_title`, `build_chapters`,
    `BUILD_MODES` and the `split_text_into_chapters` import.
  - `assemble_pages` loses `chapter_separator`. It warns when a chapter heading appears
    inside a box: "move it and the photos after it into their own box".
- **`_chapter_page_ids`** (`app.py:~2192`): drop ids that are no longer in the manifest.

## Frontend design

### Files
- **`web/src/pageBoxes.js`** (new, pure) with **`pageBoxes.test.js`**:
  - `sortFilesForUpload`: natural order only when every name matches one pattern after
    replacing digits with `#`.
  - `checkDrop`: type, extension and size, with the iPhone HEIC message.
  - Grouping and lookup: `groupBoxes`, `locate`, `neighbourId`.
  - Move planning: `planPhotoMove` (into an earlier box → its end; into a later box → its
    start), `planDrop`, `planSplit`, `planBoxMove`.
  - Optimistic updates: `applyMove` and `applyBoxOrder`, which mirror the server's seam
    reset.
  - `nextSeam` and `uploadReport`.
- **`web/src/pages/PagesPage.jsx`** becomes the container:
  - the rail and its poll, and the `?photo=` URL state;
  - the open photo's record, draft and persist, with the flushed autosave;
  - the upload queue and the write queue;
  - the header actions.
- **`web/src/components/ChapterBoxes.jsx`** (new): boxes, tiles, seam chips, the "+" tile,
  the New chapter zone, the empty state and drag-and-drop.
- **`web/src/components/PagePhotoView.jsx`** (new): built from the existing
  `PageViewer`/`PageEditor` (`PagesPage.jsx:501-632`), plus the seam control and the Move
  section.
- **`web/src/components/BuildChaptersDialog.jsx`** (new): replaces `BuildDialog`.
- **`web/src/api.js`:**
  - add `createPageBatch`, `reorderPageBatches`, `renamePageBatch`, `deletePageBatch` and
    `movePages`;
  - `buildChapters` gains `preview`;
  - remove `reorderPages`;
  - rewrite the comment at lines 50-53.
- **`web/src/guide.js` (lines 40-54):** describe chapter boxes.
- **`web/src/test/setup.js`:** add no-op `scrollIntoView` and `scrollTo`.

### Behaviour
- **Write queue.**
  - Every write that changes the rail goes through one first-in-first-out chain, with an
    optimistic update. That covers move, box operations, page delete, and seam, status and
    text saves, including the autosave flush.
  - Only the last response is applied.
  - Polls are ignored while writes are pending, and any payload with an older `rev` is
    dropped.
  - Stitch stays outside the queue, because its model call is slow and the server validates
    its writes.
- **Upload queue.**
  - One chain. The target box is fixed when a drop is queued.
  - `checkDrop` refuses the whole drop before anything is sent.
  - A duplicate never sets the drop's box.
  - A mid-drop failure (network) pauses the queue: "Stopped at IMG_4.jpg (photo 4 of 10)…
    [Try the rest again] [Cancel the rest]". The retry runs first.
  - A duplicate already in another chapter is reported: "IMG_3.jpg is already in Chapter 4".
  - A banner shows progress: "Adding 3 of 10 to Chapter 5".
  - Each job remembers its novel, in case the user switches novels mid-upload.
- **Overview.**
  - Box header:
    - Chapter N and its title (click to rename).
    - A state pill built from the blockers, and the photo count.
    - "Read n" when the box has unread photos.
    - ↑↓ for unbuilt boxes after the last built one.
    - Delete for unbuilt boxes. The confirm shows the photo count, and the request sends
      `expect_page_ids`.
  - Built, unchanged boxes are collapsed by default.
  - A built box's pill shows its chapter's translation status, or "translate again" when
    `source_changed`.
  - Tiles: a `div role=button` that is draggable, `w-28`, with a 3:4 thumbnail, the position
    number, `PageBadge` and the file name.
  - Seam chips sit between tiles. A click cycles sentence → paragraph → gap. A `chapter`
    seam shows as "heading here — split?".
  - The legacy box is a locked, collapsed header: "Chapters 1–N · built before chapter
    boxes".
- **Drag-and-drop.**
  - Tile drags use a custom MIME type with `setData` (needed for Firefox) and are cleared on
    dragend.
  - File drags land only on explicit targets: a box, the New chapter zone, the Add chapter
    button or the empty state.
  - A window-level `preventDefault` stops a missed drop from opening the image, and a toast
    says where to drop.
- **Photo view.**
  - Top bar: Back to chapters · Chapter 5 · title — photo 3 of 8 · ◀ ▶.
  - Photo on the left with zoom, editor on the right; they stack on a phone.
  - ←/→ keys step within the chapter, but not while typing, with a modifier held, or with a
    modal open.
  - The text box is read-only while Claude is reading that photo, and the text is re-fetched
    when reading finishes.
  - A segmented control sets the seam.
  - Move: Earlier / Later / Move to chapter / Split chapter here.
  - Kept from the current editor:
    - the autosave is flushed, not dropped, on navigation;
    - `has_text`/`chars` are recomputed;
    - the hint input is keyed by page id;
    - verify issues can be applied;
    - the low-Korean warning;
    - Re-read and Check go through `submitTask`.
- **Build dialog.**
  - It runs a preview when it opens, and again when "Also use photos marked to check" is
    toggled.
  - It lists what will happen:
    - will add;
    - will update, noting "translate again" for translated chapters;
    - waiting, with reasons;
    - blocked.
  - It hints "N joins not worked out yet — press Work out the joins first".
  - It shows every warning.
  - Start over sits under a `<details>`, with a preview and a danger `useConfirm`, and a
    second confirm for force.
  - After a build it shows a result view with every warning. It only closes with a toast
    when there are no warnings and nothing waiting.
  - It gets a stable `onClose` (`useCallback`), so the poll can't steal focus.

## Tests

**Backend (pytest, using the existing fixture patterns)**
- `tests/test_pages_store.py`
  - Replace the reorder tests.
  - Uploads: a page lands at the end of its box; an unknown or legacy box is refused.
  - Moves:
    - within a box and across boxes, keeping order, recounting, and never renaming files;
    - a same-spot move is a no-op that keeps every seam.
  - Seams:
    - reset exactly where the neighbour changed (move, delete, skip);
    - the first page of a box has none.
  - Box operations:
    - create (atomic with ids; after a built box → 409);
    - rename keeps punctuation;
    - order keeps the built prefix;
    - delete (built → 409, stale expect → 409).
  - `normalize`: idempotent, with 8-hex orphan ids.
  - Migration (four tests):
    - runs become boxes;
    - built pages keep their chapter, and a needs-check page rejoins its chapter;
    - a `"*"` build becomes a legacy box;
    - two loads give the same ids.
  - Every save bumps `rev`.
  - Box state, number and blockers.
  - A built box turns "changed" on text, order, membership, skip, join or label, but not
    on a status change.
- `tests/test_ocr_build.py`
  - Delete the separator, heading, single and mode tests, and the contiguous-run test.
  - Rewrite the rest on `build_boxes`:
    - one chapter per box, in box order; membership decides the chapter, not adjacency;
    - skipped pages are left out;
    - a needs-check or empty page blocks its box instead of vanishing;
    - `include=all`;
    - the build stops at the first box that isn't ready;
    - a changed built box is rebuilt in place, with the new order and `page_map`;
    - a photo moved between built boxes;
    - one photo never feeds two chapters;
    - a title-only change goes in `updated`;
    - seams are filled and written back, and an unsure seam becomes a paragraph with a
      warning;
    - a heading inside a box is warned about;
    - Start over;
    - a legacy box is never rebuilt;
    - a box reads "built" right after its build.
- `tests/test_pages_api.py`
  - Turn the reorder tests into move tests.
  - The route-capture loop covers every new literal route.
  - Uploads:
    - an upload into an earlier box lands at the end of that box;
    - an unknown box → 404;
    - a duplicate reports `existing_batch` and `existing_number`.
  - Box endpoints.
  - Structural writes return `pages` with the post-save `rev`.
  - An unknown request field → 422.
  - Stitch:
    - stays within a box;
    - keeps model decisions and gets past the cap;
    - skips a seam whose neighbour moved during the model call.
  - Built chapters follow later changes:
    - a photo added to a built chapter joins it;
    - reordering a built chapter reaches its text and `page_ids`;
    - an unchecked page holds its chapter back.
  - Build endpoint behaviour:
    - preview writes nothing;
    - pile modes are refused;
    - 409 when the chapters and boxes disagree;
    - Start over removes empty boxes and refuses boxes that aren't ready;
    - deleted page ids are filtered from a chapter's photos;
    - reading queues pages in box order;
    - the build holds the pages lock. A patched `cache_source` asserts
      `file_lock(pages_file)._is_owned()`; this is deterministic, with no threads.

**Frontend (vitest)**
- `pageBoxes.test.js`:
  - both sort branches;
  - a HEIC file refuses the whole drop;
  - orphans stay visible;
  - the edge cases of the move planners;
  - `applyMove`: an exhaustive check that the result is still a permutation and still
    grouped, with exact seam resets.
- `PagesPage.test.jsx`
  - Setup: fix the mock (`uploadPages` → `uploadPage`); use a partial `react-router-dom`
    mock plus `MemoryRouter`.
  - Overview:
    - boxes render in order, with numbered photos;
    - the empty state;
    - Add chapter;
    - built boxes can't be moved or deleted.
  - Uploads:
    - photos upload in name order into the clicked box;
    - a new-chapter drop stays one box;
    - a duplicate never redirects a drop;
    - a second drop waits for the first;
    - a HEIC drop sends nothing;
    - a failure pauses the queue and the retry runs first.
  - Moving:
    - an abandoned tile drag doesn't swallow a later file drop;
    - a tile drag calls `movePages`;
    - dropping a tile where it already is sends nothing;
    - two quick "Later" taps are sent one after the other;
    - a stale poll doesn't undo a move.
  - Photo view:
    - open a photo, then Back;
    - arrow keys stay within the chapter, and not while typing;
    - a pending edit is flushed when stepping;
    - the text appears when reading finishes.
  - Build dialog:
    - preview, waiting reasons and warnings are shown;
    - Build is enabled when the only change is `updated`;
    - Start over asks twice;
    - the `source_changed` pill.
  - The existing `submitTask` and poll tests still pass.

## Verification
1. `.venv\Scripts\python.exe -m pytest tests -q`: everything passes.
2. In `web/`, `npm test` and `npm run build`: both pass.
3. Run the real app. Close it first; `start.bat` rebuilds `web/dist` on its own.
   1. Make a test image novel with two chapter boxes of generated PNG pages.
   2. Type their text in the editor and mark it good (no paid OCR needed).
   3. Reorder photos, and move one photo to the other box.
   4. Build. The Chapters tab should show two chapters in order, and the reader's Photo view
      should show the photos in box order.
   5. Reorder a photo in chapter 1 and Build again. Chapter 1 should update in place, and no
      new chapter should appear.
   6. Check the Earlier / Later / Move to chapter controls at phone width.

## Not included, on purpose
- Inserting a new chapter before an already-built one. It needs Start over; the UI and the
  guide explain this.
- Moving several selected photos at once.
- Uploading at a chosen position. Photos land at the end of the box, then you move them.
- Merging boxes.
- Remembering which boxes were collapsed between visits.
- No commits unless asked. `web/package-lock.json` from the audit fix is also still
  uncommitted.
