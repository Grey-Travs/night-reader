# API and storage contract (authoritative)

Where the design docs disagree, this wins, then plan.md, then critique.md, then backend-design.md / frontend-design.md.

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
