# Review of the chapter-box designs (backend and frontend)

The two designs fit the brief and, together, close most of the 17 ordering risks. They are not consistent with each other, though: about ten request and response shapes disagree. Some of those mismatches fail silently. Two order-safety holes remain in the frontend.

## 1. Coverage of the 17 ordering risks

| # | Risk | Verdict |
|---|---|---|
| 1 | Page silently dropped, returns later as its own chapter | Closed. Blockers replace dropping, built boxes refresh in place, and migration puts the page back into its chapter's box. |
| 2 | Retry after a failed upload splits the chapter | Partially closed. Uploads go to a fixed box and duplicates are skipped. A later queued drop into the same box can still interleave (R2), and the common failures could be caught before anything is sent (R3). |
| 3 | Two drops interleave | Closed by the single upload queue. |
| 4 | Browser file order | Partially closed. The natural sort runs unconditionally and can scramble a correct selection order (R12). |
| 5 | Built chapters frozen | Closed by refresh in place. Inserting a chapter before a built one still needs Start over, by design. |
| 6 | Default build strips needs-check pages from built chapters | Closed. Unchanged boxes are left alone; changed boxes wait instead of dropping pages. |
| 7 | Build warnings never shown | Closed, but only once the build response contract is fixed (R5). |
| 8 | Dragging between chapters breaks them apart | Closed by `/pages/move` and grouping by box id. |
| 9 | "New chapter" chip does nothing | Closed. The chip leaves the click cycle, a heading inside a box gives a warning, and the UI offers a split. |
| 10 | Stale joins | Closed by `join_prev_id` reconciliation in `normalize` and the write-time checks in stitch. |
| 11 | Leftover drag state | Closed: drag end clears it, and the custom data type tells tile drags from file drags. |
| 12 | Chapters always added at the end | Closed: box order is now chapter order and is shown. Placing a chapter before a built one still needs Start over. |
| 13 | Missing-page check stops at 25 seams | Mostly closed. Model decisions are kept and `remaining` is returned. Pages in the wrong order that each end on a full sentence are still never flagged. |
| 14 | Wrong splits in heading/separator modes | Closed by removing those modes. |
| 15 | Reader's photo strip out of date | Closed: `page_map` is rewritten on refresh and deleted ids are filtered. Legacy chapters still show no photos. |
| 16 | Duplicate says "already here" without saying where | Closed once the field names agree (R6). |
| 17 | A poll undoes a drag | Partially closed. Polls are guarded, but concurrent writes can still reach the server out of order (R1). |

## 2. Contract mismatches between the two designs

1. **Build dry run.** The parameter is `preview` in the backend design and `dry_run` in the frontend. The response is `added: list[int]`, `refreshed`, `updated`, `waiting[{batch, number, title, built, reasons}]`, `would_redo` in the backend; the frontend expects `plan[{action: add|refresh|keep|wait|blocked}]` with `added` as a number.
2. **Changes the frontend can't apply.** The frontend dialog knows nothing about `updated` (a rename or photo-order change with the same text). It disables Build when there is nothing to add or refresh, so a box in that state shows "Changed — build to update" and can never be built.
3. **Duplicate upload fields.** The backend returns `existing_batch` and `existing_number`; the frontend reads `res.batch`. With the backend's shape every duplicate is counted as "already in this box", so the "already in Chapter 4" notice never appears.
4. **What a write returns.** The frontend expects every write to return `pages` (the fresh summary). The backend returns `{ok, changed, rev}`, `{batch: row, rev}`, `{ok, rev}` or `{ok, removed, rev}`.
5. **Creating a box.** The frontend sends `POST /pages/batches {after, ids}` for "Split chapter here" and for dropping a tile on "New chapter". The backend model only has `{label}`. Pydantic 2 (`pydantic>=2.6`) ignores unknown fields by default, so the server would silently create an empty box at the end, move no photos, and the toast would still say "Chapter 6 now starts at this photo".
6. **Box delete.** The frontend sends `{expect_page_ids}`; the backend takes no body and ignores it.
7. **Legacy box shape.** The backend sends `state: "legacy"` and no chapter range. The frontend expects `state: 'built'` plus `chapters: [first, last]`. They also disagree on renaming: the frontend forbids it, the backend allows it.
8. **Stitch response.** The backend calls the count `remaining`; the frontend calls it `left`.
9. **`rev`.** The backend provides it; the frontend ignores it and runs its own epoch guard.
10. **Box row fields.** The backend sends `page_ids`, `blockers` and `title`; the frontend wants `page_ids` and `blockers` removed.
11. **Smaller items:**
    - api method names differ (`createBatch` in the backend's §7, `createPageBatch` in the frontend). The frontend's names should win, since it owns `api.js`.
    - Box-order refusals are 409 in the backend and 400 in the frontend. Harmless.
    - Orphan box ids aren't guaranteed to be 8 hex characters, but the frontend wants route ids checked against that pattern (R15).

## 3. Required changes, most important first

**P0: these break the contract or let the order get mixed up**

**R1. Send the frontend's changes one at a time.**
- **Problem:** the frontend's guard (§3.1) assumes that "the last write to finish carries every write before it". That is false.
  - Sync endpoints run in a threadpool. `file_lock` is a plain `threading.RLock` (`translation_bot/locks.py:29-41`), which doesn't serve waiters in order.
  - A phone link can also reorder requests.
- **Scenario:** three quick "Later" taps send anchors X3, X4, X5, each worked out from the on-screen order. The server can apply them as X5, X3, X4. The photo ends up in a different place than the screen showed, or an anchor isn't in the box yet and the request fails with a 400.
- **Fix:**
  - Put every page change through one first-in-first-out queue, applied optimistically on screen: move, box create/order/rename/delete, page delete, seam, status, text and the autosave flush.
  - Apply only the response of the last write. Ignore polls while the queue isn't empty.
  - Use the backend's `rev` to drop any older payload. This can replace the epoch/ticket/inflight refs.
  - Keep stitch out of the queue: its model call would hold up moves for many seconds, and the server already checks its writes (backend §3.3). Treat its response like a poll.

**R2. Pause the upload queue when a drop fails, and run the retry first.**
- **Evidence:** frontend §3.4. On an error, `runUpload` stops that job and the chain goes straight on to the next job. "Try the rest again" adds the retry at the end of the queue.
- **Scenario:** drop A (IMG_1–10 into Chapter 5) fails at IMG_4. Drop B (IMG_11–20, also into Chapter 5) is waiting and runs next. Chapter 5 ends up as 1, 2, 3, 11–20, and the retry then adds 4–10 at the end.
- **Fix:**
  - A stopped job pauses everything queued after it until the user picks Retry, Skip this drop or Cancel.
  - The retry goes to the front of the queue.
  - This also protects new-chapter drops: if drop A fails before its box exists, drop B would otherwise create its box first and the two chapters would swap places.

**R3. Check the whole drop before sending anything.**
- **Evidence:** the server refuses HEIC, video, PDF and anything over 25 MB, one file at a time (`server/app.py:1386-1401`, `server/pages.py:77-96`). The client only filters out empty files (`PagesPage.jsx:165`, and the same in frontend §3.4), so the drop stops partway and leaves half a chapter.
- **Fix:** before any upload, check each file's extension, type and size.
  - If any file is HEIC/HEIF, not JPEG/PNG/WebP, or over 25 MB, refuse the whole drop and list the files, with the iPhone "Most Compatible" fix.
  - Skip and report empty files and OS junk files.
  - After this, only network errors can stop a drop midway, and "Try the rest again" is right for those. "Try the rest again" is pointless for a 400 or 413 anyway.

**R4. Create a box with its photos in one request.**
- **Fix:** `POST /pages/batches {label?, after?, ids?}`:
  - Create the box and move the photos inside one `mutate_pages` call.
  - `after` must be the last built box or any unbuilt box; anything else is a 409.
  - Return `{batch: row, pages}`.
- **Also:** give the new request models (`NewBox`, `MovePages`, `BoxOrder`, `BoxRename`, `DeleteBox`, `BuildChapters`) `model_config = ConfigDict(extra="forbid")`, as `server/app.py:215` already does. Contract drift then fails loudly with a 422 instead of being silently ignored.

**R5. Agree one build response, and let a title-only change be built.**
- **Fix:**
  - Use the backend's `preview` flag.
  - Use `{chapters, added: [int], refreshed: [int], updated: [int], waiting: [{batch, number, title, built, reasons, after_chapter?}], warnings, project?}` for both the real build and the preview.
  - Add an explicit `after_chapter` field (or `kind: "blocked"`) on waiting entries, so the client doesn't have to pattern-match reason text to tell "blocked behind chapter 6" from "waiting".
  - The frontend builds its plan from this. Build is enabled when `added`, `refreshed` or `updated` is non-empty. Toasts use `added.length`.
  - Drop `would_redo`; neither design defines or uses it.

**R6. Duplicate uploads: keep the backend's `existing_batch` and `existing_number`.**
- **Why:** it is safe against an old cached client still doing `batch = batch || res.batch` (`PagesPage.jsx:175`). The app is used from a phone, so a stale bundle is realistic.
- **Frontend changes:** `uploadReport` and test #9 read these fields. `existing_number: null` (a legacy photo) needs its own wording.

**R7. Every write returns the fresh summary (`pages`, including `rev`).**
- **Precedent:** stitch already does this (`server/app.py:1655`).
- **Build it after the `with` block.** `upload_page` returns from inside `mutate_pages` (`server/app.py:1404-1428`), and the save, which bumps `rev`, runs only on exit. A response built inside the block therefore carries the old `rev`.

**R8. Box delete accepts `{expect_page_ids?}` and returns 409 on a mismatch.**
- **Problem:** without it, a photo uploaded between the confirm dialog and the delete is removed without being shown ("Delete 8 photos?" removes 9).

**R9. Keep `blockers` in the box rows and build the pill text from them.**
- **Problem:** the backend's `box_state` maps `no-text` to `to-check` and `failed` to `unread` (§1.4). The frontend's pill counts come from page statuses, so a box held back only by an empty-text page reads "0 to check".
- **Also:** an all-skipped box shows "No photos yet" although it has photos. Give it its own state or label: "All photos skipped — delete this box".
- **Can go:** `page_ids` can be dropped from the rows.

**R10. Legacy box row.** Send `state: "legacy"`, `number: null`, `chapters: [first, last]`, and refuse rename. Name the stitch field `remaining`, and use the frontend's api method names.

**P1: correctness**

**R11. "Start over" must not quietly shorten the novel.**
- **Problem:** backend §5 says chapters after the first unready box "drop out of source.json".
  - That rewrites `source.json` shorter, re-pads the chapter file names (`server/app.py:559-610`, `1753-1762`), and leaves exports incomplete.
  - Worse, a built box emptied by moving its photos becomes an unbuilt `empty` box on Start over. The build stops there, and every chapter after it disappears.
- **Fix:**
  - Start over removes empty boxes, built ones included.
  - It refuses with a 400 listing the waiting boxes if any box with photos isn't ready.
  - The frontend runs a preview with `append: false` inside the confirm dialog.
  - This is also the only way out of the documented dead end: moving every photo out of a built chapter leaves the receiving box waiting forever under the one-photo-one-chapter rule.

**R12. Natural sort only when the names follow one pattern.**
- **Rule:** replace each run of digits with `#`. Sort only if every file then has the same name; otherwise keep the selection order.
- **Why:** frontend §2 sorts unconditionally. Converted or renamed phone files can have names that aren't in sequence, and sorting them would scramble a selection that was already right.
- **Tests:** add one for each branch.

**R13. Show when a built chapter's translation is out of date.**
- **Problem:** `chapter_row` reports status `pending` when `source_changed` is true (`server/app.py:765-771`). After a refresh in place, a translated chapter would read "Not translated yet" (frontend §4 pill table).
- **Fix:** when `chapters[k].source_changed`, show "Changed since translated — translate again".

**R14. Autosave flush and switching novels.**
- **Flush:** "Back to chapters" runs a quiet `loadRail` while the flush (a direct `api.savePage` call, `PagesPage.jsx:156-161`) may still be in flight, so the box pill shows the old state. Send the flush through the R1 queue and reload after it.
- **Novel switch:** PagesPage stays mounted when the `:pid` changes (`ProjectLayout.jsx:97-101`).
  - Only render ghost tiles and banners for upload jobs whose `job.pid` is the current `pid`.
  - Skip the upload report if the user has switched novels.

**R15. Orphan box ids must be 8 hex characters.** `normalize` step 2 reuses the raw `batch` value as the box id. Only keep it if it matches `^[a-f0-9]{8}$`; otherwise derive one with `_derive_id`.

**R16. Migration (low priority, there is no v1 data).**
- **Problem:** v1 built empty seams as paragraph breaks (`server/ocr_build.py:62`). The new `fill_seams` would turn them into sentence joins, so untouched translated chapters would come back as `refreshed`, show as `pending`, and need re-translating (`server/app.py:656-677`).
- **Fix:** in `migrate_v1`, set empty seams on pages in built boxes to `paragraph` (source `migrated`).

**R17. Blocker and warning text that says what to do.**
- `no-text`: "IMG_5 has no text — if it's a picture or blank page, mark it Skip".
- `failed`: "read it again, type it, or Skip".
- Unsure seams: one warning per chapter, not one per seam.
- One-photo-one-chapter waits: name why the other chapter can't be updated.

**Checked and found sound:**
- Route order; both designs list every new route before `/pages/{page_id}` (line 1795).
- `normalize` against the OCR worker: `seq` never changes and results are written by id.
- Migration is deterministic and gated by `version`.
- `content_hash` covers paragraphs only (`translation_bot/docs_extract.py:71-78`), so a rename doesn't mark a translation stale.
- The "box N becomes chapter N" invariant, given the stop rule, the built-first `normalize` and the 409 guard.
- `load_text_chapters` returns `[]` for a corrupt `source.json` (`server/projects.py:197-206`), so the backend's 409 on a chapter-count mismatch is the right guard. Its message should name Start over.

## 4. Optional simplifications

- **Frontend:** once R1 is in, drop the epoch/ticket/inflight refs.
- **Frontend:** defer these to a later round:
  - ghost tiles (a per-job banner "Adding 3 of 10 to Chapter 5" is enough);
  - `useFileDragActive` outlines;
  - the "Next to check" button;
  - `mergeBox`;
  - the coarse-pointer seam rule.

  Keep the window-level `preventDefault` that stops a missed drop opening the image.
- **Frontend:** keep the legacy UI to a locked header with no "+" tile and no dragging.
- **Backend:**
  - Drop the optional threadpool upload.
  - Trim the v1 migration tests to four: runs become boxes; built pages keep their chapter; a `"*"` build becomes a locked legacy box; two loads give the same ids.
  - Make the threaded build-vs-upload test deterministic (an Event plus a timeout) or drop it, because threading tests tend to be flaky.
- **Backend (efficiency):** `resolve_page_file` loads the whole manifest for every image request, and with this design it would also migrate and normalize each time. Give image serving a cheap path that skips `normalize`.

## 5. Missing things the user would expect

- **Fixing a chapter boundary on a phone in one tap:** "Move this and the photos after it to the next chapter" and "Move this and the photos before it to the previous chapter". Each is one `/pages/move` call with ordered ids. Add a "Move to position N" select for long boxes; Earlier/Later is one request per step.
- **One-tap "New chapter from photos":** open the file picker synchronously with the new-box target, so it works on iOS. This avoids abandoned empty boxes, which block every later build.
- **A plain explanation** in the disabled-button tooltip and in the guide that inserting a chapter before a built one needs Start over.
- **A build dialog hint:** "N joins not worked out yet — Work out the joins first". Otherwise the build's fill warnings invite stitching after the chapters are translated, which forces re-translation.

## 6. Tests to add

**Backend:**
- Write responses carry `pages` and the post-save `rev`.
- `POST /pages/batches` with `after`/`ids` is atomic, and returns 409 when it would place a box before a built one.
- A delete with stale `expect_page_ids` returns 409 and deletes nothing.
- Start over removes empty boxes and refuses when a box with photos isn't ready.
- A box stays `built` after a build followed by a stitch with no model.
- Deleting the last photo of a built box leaves its chapter text unchanged and gives an `empty` blocker.
- OCR worker writes (`_apply_page_result`, `_set_page_status`) keep page order and seams.
- Text that is only invisible characters gives a `no-text` blocker.
- A non-hex orphan batch gets a derived 8-hex id.
- `/pages/move`, `/pages/batches/{id}` and `/pages/batches/{id}/delete` are in the route-capture loop.
- An unknown field in a request returns 422.

**Frontend:**
- Two quick Later taps: the second `movePages` is sent only after the first resolves.
- A failed drop pauses a later drop into the same box, and the retry runs first.
- A drop containing a `.HEIC` sends nothing.
- A duplicate with `existing_batch` produces the "already in Chapter N" notice.
- Build is enabled when the plan has only `updated` boxes.
- A chapter with `source_changed` shows the re-translate pill.
- Arrow keys are ignored while a modal is open.
- Sorting covers both branches of R12.

### Critical Files for Implementation
- C:/Users/Mommy/Documents/night-reader/night-reader/server/pages.py
- C:/Users/Mommy/Documents/night-reader/night-reader/server/app.py (upload 1371-1428, stitch 1606-1655, build 1658-1792, update_page 1825-1861, `_chapter_page_ids` 2192-2196)
- C:/Users/Mommy/Documents/night-reader/night-reader/server/ocr_build.py
- C:/Users/Mommy/Documents/night-reader/night-reader/web/src/pages/PagesPage.jsx (plus the new web/src/pageBoxes.js and web/src/components/BuildChaptersDialog.jsx)
- C:/Users/Mommy/Documents/night-reader/night-reader/web/src/api.js