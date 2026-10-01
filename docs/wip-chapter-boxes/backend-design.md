# Backend design: chapter boxes (pages.json v2)

## 0. Changes to the draft contract

1. **Seam reset is stored on the page, not done per endpoint.** Each page gets a new field, `join_prev_id`: the id of the page its seam was decided against. `normalize()` clears any seam whose `join_prev_id` is not the page's current previous non-skipped page in the same box.
   - This covers move, delete, box delete, skip and un-skip, migration, and any path added later.
   - Moving a page to its own spot changes no neighbour, so no seam resets. That is the no-op the draft asks for.
2. **New box state `legacy`.** It holds pages from a v1 heading, separator or single build. The box is locked and has `number: null`.
3. **`built_sig` and the build response.**
   - `built_sig` hashes the stored inputs (see §1.4).
   - Build writes the seams it fills back onto the pages, so the hash stays consistent after a build.
   - Migrated built boxes get `built_sig: null` and show "changed" once.
   - The response separates `refreshed` (Korean text changed, so a re-translate warning) from `updated` (only the title, photo list or record changed).
4. **New "one photo, one chapter" build rule.** A box waits if any of its pages is still in the built `page_map` of a different chapter that this build isn't rewriting. Without this rule, moving a photo out of a built box that isn't ready puts the same text in two chapters.
5. **Box creation and ordering.**
   - `POST /pages/batches {label}` always adds at the end. I dropped `after`; the order endpoint covers placement.
   - `/pages/batches/order` takes the full list of box ids. The built boxes at the front must stay where they are.
6. **Duplicate upload response uses `existing_batch` and `existing_number`, not `batch`.** Today's client does `batch = batch || res.batch`, which would send the rest of a drop into the duplicate's box.
7. **New `rev` save counter** in pages.json, GET /pages and every mutation response. This is the server side of the poll sequence guard.
8. **Build API changes.**
   - New `preview: true` (dry run).
   - `added` becomes `list[int]`; new `updated`, `waiting` and `would_redo` fields.
   - Any `mode` other than `batch` or `boxes` gets a 400.
   - `include` is validated.
9. **`/pages/reorder` and `pages.reorder` are removed.** `/pages/move` replaces them. A novel-wide permutation can't say which box a page belongs to, and normalize would silently regroup it.
10. **Stitch extras.** It skips pairs where either side has no text, skips legacy boxes, re-checks both texts when writing, and returns `remaining`.
11. **Build returns 409** when source.json's chapter count differs from `build.chapters` (a corrupt source.json or a half-written earlier build). Today it would append at the wrong chapter number.
12. **Empty boxes.** An empty built box stays, because it is the chapter's handle. An empty legacy box is removed automatically.

## 1. server/pages.py

### 1.1 Data and storage

- **Box record:** `{id, label, at, count, chapter: int|None, built_sig: str|None}`, plus `legacy: True` on legacy boxes only.
- **Page record:** gains `join_prev_id: str|None`.
- **`new_doc` (138-140):** `{"version": 2, "rev": 0, "next_seq": 1, "pages": [], "batches": [], "build": None, "totals": {...}}`.
- **`load_pages` (143-169):** after the existing setdefaults, call `setdefault("rev", 0)`. Run `migrate_v1(doc)` if `version < 2`, then always run `normalize(doc)`. Both are pure and deterministic, so a read can never mint a different box id from the next read.
- **`save_pages` (172-174):** `normalize(doc)`, then `doc["rev"] = rev + 1`, then write. Every doc in memory or on disk is therefore normalized.
- **New `class BoxError(Exception)`** with `(status: int, message: str)`. app.py maps it to `HTTPException`.

### 1.2 Helpers

- **`clean_title(text, limit=120)`:** replaces every character whose `unicodedata.category` starts with `"C"` with a space, folds whitespace, trims, then truncates. It keeps `: ! ? " '` (`safe_label` at 199-202 strips them). Used for box labels only; `safe_label` stays for page names.
- **`page_has_text(p)`:** `bool(_strip_invisibles(p.text).strip())`, imported from `translation_bot.docs_extract`. Used by `summary().has_text` and by the blockers. A ready box can then never assemble to an empty chapter.
- **Small helpers:**
  - `live_pages(pages)`: non-skipped pages.
  - `is_built(box)`: true if `chapter` or `legacy` is set.
  - `find_box(doc, id)`.
- **`box_pages(doc) -> list[(box, pages)]`:** pages grouped by box id, in box order.
- **`seam_neighbours(doc) -> {page_id: prev_id|None}`:** for each non-skipped page, the previous non-skipped page in the same box (None for the first page of a box).
- **`clear_seam(p)`:** sets `join_prev=""`, `join_prev_source=""`, `join_reason=""`, `join_glue="space"`.
- **`set_seam(p, *, kind, glue, source, reason, prev)`:** writes the four fields plus `join_prev_id=prev`.
- **`chapter_count(doc)`:** `max(build.chapters or 0, max(box.chapter))`.
- **`box_numbers(doc) -> {id: int|None}`:** built boxes use `chapter`; legacy boxes get None; unbuilt boxes get `chapter_count + their position among unbuilt boxes`.
- **`box_title(box, live, number)`:** the label, else the first live page's heading, else `f"Chapter {number}"`. This moves here from `ocr_build._batch_title` (106-113) so `summary` can show it without an import cycle.

### 1.3 Mutations

Each mutation ends with `normalize(doc)`. Refusals raise `BoxError`.

- **`create_box(doc, label="") -> dict`:** appends a box at the end with a fresh `uuid[:8]`. `new_batch(doc, label) -> str` stays as a thin wrapper.
- **`rename_box(doc, id, label)`:** 404 if the id is unknown. Renaming a built box changes its signature, so the next build updates the chapter title.
- **`reorder_boxes(doc, ids)`:**
  - 400 if `ids` isn't a permutation of all box ids.
  - 409 if `ids[:k]` differs from the current built prefix of length k.
- **`delete_box(doc, id) -> removed pages`:** 404 if unknown; 409 if `is_built`. Otherwise it drops the box and its pages.
- **`add_page(...)` (231-267):** now strict. Unknown box gives 404, legacy box gives 409. It adds `join_prev_id: None`, appends, and normalizes, so the page lands at the end of its box. `_touch_batch` (223-228) is removed because normalize recounts.
- **`move_pages(doc, ids, box_id, before=None) -> bool`** (True if anything changed):
  1. Refusals:
     - 404 if the box is unknown.
     - 409 if the target box, or any source page's box, is legacy.
     - 400 if `ids` is empty.
     - 404 if any id is unknown. Nothing is moved.
     - 400 if `before` is not a page in the target box.
  2. Deduplicate `ids`, keeping their order.
  3. If `before` is one of the moving pages, the anchor becomes the next page after it in that box that isn't moving (or the end).
  4. Remove the moving pages, set their `batch`, and insert them as a block before the anchor. With no anchor, insert after the box's last remaining page, or at the end of the list if the box is empty (normalize then sorts it into place).
  5. Normalize, then compare `(id, batch)` lists before and after.
- **`delete_pages` (282-290):** same behaviour plus normalize.
- **Removed:** `reorder` (270-279).

### 1.4 State and signature

```python
def box_signature(box, pages) -> str:
    rows = [[p["id"], (p.get("text") or "").strip(),
             "" if i == 0 else (p.get("join_prev") or ""),
             "" if i == 0 else (p.get("join_glue") or "space"),
             (p.get("heading") or "").strip()]
            for i, p in enumerate(live_pages(pages))]
    blob = json.dumps([(box.get("label") or "").strip(), rows],
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
```

- **What it detects:**
  - text edits;
  - reorders (row order);
  - membership changes, including skip and un-skip (the live ids);
  - join changes (`join_prev` and glue);
  - title changes (the label and the first page's heading).
- **What it ignores:** status, confidence, notes, the join's source and reason, and page names. None of these change the built chapter.
- **Why the stored joins match the build:** build writes back every seam it fills before hashing (§2). A later stitch that agrees with the fill therefore doesn't trigger "changed".

**`box_blockers(pages, *, include="approved")`** returns `[{kind, count, page_ids, message}]` in this order:

| kind | when |
|---|---|
| `empty` | the box has no photos |
| `all-skipped` | every photo is skipped |
| `reading` | pages queued or `ocr-running` |
| `unread` | pages with status `new` |
| `failed` | pages that failed to read |
| `no-text` | pages that are ok/edited/needs-check but have no visible text |
| `to-check` | pages with status `needs-check`; only when `include == "approved"` |

**`box_state(box, pages, blockers)`:**
- `legacy` for a legacy box.
- For a built box: `built` if the signature equals `built_sig`; otherwise `changed` with no blockers, or `changed-waiting` with blockers.
- For an unbuilt box, the first match wins: `empty`/`all-skipped` → `empty`; `reading` → `reading`; `unread`/`failed` → `unread`; any other blocker → `to-check`; none → `ready`.

**`box_rows(doc)`** gives one row per box: `{id, label, title, at, count, page_ids (every page in order, skipped ones too), chapter, number, state, blockers, legacy}`.

**`summary` (303-318)** returns the existing fields with `batches` replaced by `box_rows(doc)`, plus `chapter_count` and `rev`. States are always computed with `include="approved"`.

### 1.5 `normalize(doc)`

Idempotent, O(n log n), no clock or uuid use.

1. Drop non-dict pages and boxes, and any repeated box id. Set defaults for the box fields. Set `chapter` (and `built_sig`) to None when it isn't a positive int, repeats an earlier box's chapter, or sits on a legacy box.
2. Adopt orphan pages (pages whose `batch` isn't a box): one new unbuilt box per distinct unknown value. Its id is that value if non-empty and free, else `_derive_id("orphan|"+first_page_id)`. `_derive_id` takes the first free 8-hex window of the seed's sha256.
3. Remove legacy boxes that have no pages.
4. Put built and legacy boxes first and unbuilt boxes after, keeping the relative order within each group.
5. Stable-sort pages by box position.
6. Recount each box.
7. Reconcile seams: for each `(id, prev)` in `seam_neighbours(doc)`, if `page.join_prev_id != prev`, call `clear_seam(page)` and set `join_prev_id = prev`. This clears seams the user set too.

### 1.6 `migrate_v1(doc)`

Pure and deterministic. It is persisted by the first save, and `version = 2` stops it running again.

1. Build `chapter_of[id] = K` from the numeric `build.page_map` keys (the lowest K wins). Then `legacy_ids = (used_page_ids ∪ page_map["*"]) − chapter_of`.
2. For every non-skipped page in v1 global order, set `join_prev_id` to the previous non-skipped page. v1 stitched across batches, so that is what its seam was decided against. Normalize then clears the seams whose neighbour changed.
3. Walk the pages in v1 order, tracking contiguous runs of the same `batch` and the box the current run is filling:
   - **A page built into chapter K:** use the box already made for K. Otherwise adopt the current run's box if it is unbuilt, not legacy and in the same run (the unbuilt pages that opened chapter K's run). Otherwise make a new box. Set `chapter = K`.
   - **A legacy page:** goes into one shared legacy box (`legacy: True`, label ""). The run's box resets, so unbuilt pages after it start a fresh box.
   - **An unbuilt page:** joins the run's box; a new run makes a new box. A page that missed a v1 build because it was `needs-check` therefore sits back inside its chapter's box, and the next build adds it to that chapter in place. This repairs ordering risk #1 instead of making it a chapter at the end.
4. **Making a box:**
   - The id is the v1 batch id if it is non-empty and not yet taken, else `_derive_id(f"{batch}|{first_page_id}")`.
   - `label` and `at` are copied from the v1 batch (`at` falls back to the page's `added_at`).
   - `count` starts at 0; `chapter` and `built_sig` start as None.
5. **Ordering:**
   - Boxes stay in creation order (first appearance), except that numbered boxes are re-sorted by chapter within the slots they occupy.
   - Pages are concatenated in box order.
   - Empty v1 batches simply produce no box.
   - Finally, `normalize`.
6. **`built_sig` stays None** on migrated built boxes, so they show changed or changed-waiting once. The next build regenerates each chapter and compares it with source.json; an identical chapter goes into `updated`, never into the re-translate list.

Edge cases this handles:
- A v1 run that maps to two chapters (fragments from the old bug) splits into one box per chapter, matching what is actually built.
- v1 pages that have no `batch` at all (as in the `test_bundle_images.py` fixture) form ordinary runs.

## 2. server/ocr_build.py

- **Remove:** `BUILD_MODES` (22), `usable_pages` (25-40, the silent page drop), `_batch_runs` and `_batch_title` (89-113), `build_chapters` (116-177), and the `split_text_into_chapters` import.
- **`assemble_pages(pages)` (43-86):** drop `chapter_separator`. A `chapter` join inside a box still adds a blank line and restores the heading. It also warns: "{label} opens a chapter heading inside this box — joined as a new paragraph. If it's the next chapter, move it and the photos after it into their own box."
- **`fill_seams(live, *, number) -> warnings`:** handles seams whose `join_prev == ""` only.
  - It runs `propose_join(prev.text, cur.text, prev, cur)`.
  - When confident: `set_seam(kind, glue, "auto", reason, prev=prev.id)`.
  - When unsure (`needs_model`): `set_seam("paragraph", "space", "auto", "Not sure how these pages join — new paragraph; 'Work out the joins' can check it")`, plus the warning "Chapter N: not sure how {label} joins the page before it…".
  - It writes back to the page records.
- **`box_chapter(box, live, number)`:** returns `(Chapter, warnings)` from `assemble_pages(live)` and `make_chapter(number, box_title(...), text)`. Warnings are prefixed "Chapter N: ".
- **`BoxBuild`** dataclass: `chapters` (the full list), `added`, `refreshed`, `updated`, `waiting`, `warnings`, `page_map`.
- **`build_boxes(doc, existing, *, include, append) -> BoxBuild`:** no I/O; mutates `doc` and `existing` in place; expects a normalized doc.
  1. **Start over (`append=False`):** set every box's `chapter`/`built_sig` to None and clear `legacy`; `existing = []`; `page_map = {}`. Otherwise `page_map` is the numeric entries of `build.page_map`.
  2. **Built boxes to rewrite:** take built, non-legacy boxes whose signature ≠ `built_sig`. If K > `len(existing)`, the box waits ("missing from the chapter list; use Start over"). If it has blockers, it waits with its blocker messages. Otherwise it becomes a candidate.
  3. **One photo, one chapter (fixpoint):** `rewriting` is the set of candidate chapters. Any candidate that has a live page in `page_map[k]`, where k is neither its own chapter nor in `rewriting`, waits with "{name} is still part of chapter k's text, and chapter k can't be updated yet". Repeat until nothing changes.
  4. **Rewrite in place:**
     - Fill the seams, then call `box_chapter(box, live, K)`.
     - If the paragraphs differ from `existing[K-1]`, add K to `refreshed`; otherwise add it to `updated`.
     - Set the title and paragraphs, `page_map[str(K)] = live ids`, and `built_sig = box_signature(...)` (computed after the fill).
  5. **Unbuilt boxes, in box order:**
     - `number = len(chapters) + 1`.
     - The first box with blockers, or holding a page another chapter still owns, stops the build. It waits with its reasons; every later unbuilt box waits too, with "Comes after chapter N, which isn't ready yet" and its own projected number.
     - Each box before that is filled, built, appended to `chapters`, and given `chapter`, `built_sig` and its `page_map` entry; its number goes into `added`.
  6. **Waiting entries** look like `{"batch", "number", "title", "built": bool, "reasons": [str]}`.

## 3. server/app.py

### 3.1 Routes

All new routes go after `delete_pages` (1467) and before GET `/pages/{page_id}` (1795):

```
POST /pages/move                       MovePages {ids: list[str], batch: str, before: str|None=None}
POST /pages/batches                    NewBox {label: str=""}
POST /pages/batches/order              BoxOrder {ids: list[str]}      # MUST precede /batches/{batch_id}
POST /pages/batches/{batch_id}         BoxRename {label: str}
POST /pages/batches/{batch_id}/delete
```

- Each route is `require_images_project` plus `with _box_errors(), pages_mod.mutate_pages(pid) as doc:`.
  - `_box_errors()` is a contextmanager that maps `BoxError` to `HTTPException(status, message)`.
  - It must be the outer of the two, so mutate_pages exits without saving first.
- Responses:
  - move: `{ok, changed, rev}`
  - create: `{batch: row, rev}`
  - rename: `{batch: row, rev}`
  - order: `{ok, rev}`
  - delete: `{ok, removed, rev}`; files are unlinked outside the lock, as `delete_pages` does at 1461-1466.
- Delete `ReorderPages` and `reorder_pages` (1439-1449).
- Update the route-order note (1351-1354).

### 3.2 Uploads

**`upload_page` (1371-1428):**
- **Duplicate check (unchanged, still first):** returns `{page, duplicate: True, existing_batch, existing_number, total}`.
- **MAX_PAGES:** unchanged.
- **`batch=""`:** `new_batch(doc, label)` (a new box at the end). A non-empty `batch` goes straight to `add_page`, whose `BoxError` gives 404 for an unknown box and 409 for a legacy one. An unknown id never becomes a new box.
- **Response:** `{page, duplicate: False, batch, number, total, rev}`.
- **Optional:** move the locked section into `run_in_threadpool`, so a running build or a 25 MB write doesn't stall the event loop.

### 3.3 Stitch

**`stitch_pages` (1606-1655):**
- **Snapshot (unlocked):** `snap_prev = seam_neighbours(doc)`, iterated in story order.
- **Skip a pair when:**
  - `prev` is None;
  - the box is legacy;
  - `join_prev_source` is `"user"` or `"model"`;
  - either page has no text.
- Run `propose_join` on the rest; unsure seams are capped at `max_model_seams` as today. Because model decisions are now kept, each run makes progress past the cap.
- **Write (under the lock):** skip a page if `seam_neighbours(live)[id] != snap_prev[id]`, if the live source became `"user"`, or if either page's text differs from the snapshot. Otherwise update it with `join_prev_id = snap_prev[id]`.
- **Response:** adds `remaining = len(unsure) - asked`. `seams` is the number of seams actually written.

### 3.4 Build

**`BuildChapters` (1658-1663):** `include: str = "approved"`, `append: bool = True`, `force: bool = False`, `preview: bool = False`, `mode: str | None = None`.

**`_refresh_built_chapters` (1666-1698):** deleted; `build_boxes` replaces it.

**`build_pages_into_chapters` (1701-1792):**

1. **Validate:** `mode ∉ {None, "", "batch", "boxes"}` gives 400 ("each chapter box builds one chapter; reload the page"); `include ∉ {approved, all}` gives 400.
2. **Preview:** `doc = load_pages(pid)` (unlocked), `existing = pj.load_text_chapters(pid)`, run the same checks and `build_boxes`, and return the result with `preview: True` and `would_redo`. It writes nothing and never returns the "nothing to build" 400.
3. **Real build:** the whole thing runs inside `with pages_mod.mutate_pages(pid) as doc:`.
   - `existing = pj.load_text_chapters(pid)`.
   - If `append` and `int(build.chapters or 0) != len(existing)`, return 409.
   - If not `append`, keep today's finished-chapter check (1732-1738) and its message.
   - `res = ocr_build.build_boxes(doc, existing, include=..., append=...)`.
   - If `added`, `refreshed` and `updated` are all empty, return 400, so nothing is saved. The message is "No chapter is ready to build yet — chapter N: {first reason}", or "Every chapter is already up to date.".
   - `pj.cache_source(pid, res.chapters)` and `_chapter_cache.pop(pid)`.
   - Set `doc["build"] = {mode: "boxes", at, chapters: len, warnings, page_map: res.page_map, used_page_ids}`.
     - `used_page_ids` is the union of the `page_map` values plus the legacy box's page ids. Older app versions read it.
     - The `"*"` key is dropped.
4. **After the lock:** write `project.json`, then `_normalize_chapter_padding` and `_invalidate_consistency`.
   - Writing `project.json` inside the lock means an `OSError` there would leave source.json written and pages.json not.
5. **Response:** `{ok, chapters, added, refreshed, updated, waiting, warnings + the re-translate warning for refreshed (wording kept, still containing "re-translate"), project, preview: False}`.

### 3.5 Page edits and the reader's photo strip

**`update_page` (1825-1861):**
- When `join_prev` is set, also set `rec["join_prev_id"] = seam_neighbours(doc).get(page_id)`.
- When `status` is set, call `normalize(doc)` before `updated = dict(rec)`, so the response shows seams reset by a skip or un-skip.

**`_chapter_page_ids` (2192-2196):** filter `page_map[str(index)]` to ids still present in the doc. Also update the comment at 2184-2187.

## 4. Concurrency

- **Today's race:** build reads the doc unlocked (1711) and computes from that snapshot. An upload, move or OCR result written in between makes `page_map` and `used_page_ids` stale. Two builds can both read the same `existing` and append twice.
- **Fix:** run the whole build (manifest read → source.json read → compute → source.json write → build record) inside `mutate_pages`. `file_lock` is one RLock per path that every pages writer already takes, so builds serialize with uploads, moves, box operations, page edits, OCR result writes and other builds. This is the cheap fix.
- **Cost:** for the length of a build (pure Python plus one atomic write), async endpoints that take the lock on the loop thread block the event loop. That affects upload, the stitch write, and the worker's status writes. It is acceptable here; the optional threadpool move in §3.2 removes it for uploads.
- **Stitch:** holds no lock during the model call. Its writes are validated against the live neighbour, text and source (§3.3).
- **Reads** (GET /pages, images, `_chapter_page_ids`) stay unlocked; atomic writes mean they always see a whole file.
- **Lock order:** padding and consistency invalidation run after the pages lock is released, so there is no pages-lock-then-chapter-lock ordering.

## 5. Edge cases

- **Last photo deleted or moved out of a built box:**
  - The box stays, in state changed-waiting with an `empty` blocker: "Chapter N has no photos left. A built chapter can't be removed without renumbering — add photos back or Start over."
  - Chapter N is left as it is.
  - The reader's photo ids are filtered.
  - The ownership rule stops the moved photos from appearing in another chapter as well.
- **Skipped pages:** left out of seams, the signature and chapters; never a blocker; still shown in `page_ids` and `count`.
- **A box where every page is skipped:** an `all-skipped` blocker. An unbuilt one stops later boxes (box N must be chapter N).
- **Empty unbuilt box:** blocks the boxes after it, with a clear reason. It can be deleted. Clients should create a box lazily (`batch=""` on the first successful upload), or delete a box they pre-created that ended up empty.
- **Legacy (`"*"`) pages:**
  - Locked: no upload into the box, no moves in or out, and the box can't be deleted.
  - Individual photos can be deleted; an emptied legacy box is removed.
  - Never stitched or built.
  - Start over clears `legacy` and builds them as normal boxes.
- **MAX_PAGES:** unchanged. Moves never add pages.
- **Moves involving built boxes:** allowed in both directions, except legacy. The affected boxes become changed, and the next build refreshes them in place under the ownership rule.
- **Unknown ids in a move:** 404 for the whole request; nothing moves.
- **Move to the same spot:** returns `changed: False` and resets no seams.
- **Start over that stops early:** chapters after the stop drop out of source.json (they rebuild once their boxes are ready). The force check protects translated chapters, and preview shows exactly what will happen.
- **Renaming a built box:** the box shows changed; the next build sets the new title and lists the chapter under `updated`. `content_hash` covers paragraphs only, so the translation isn't marked stale.

## 6. Tests

### tests/test_pages_store.py

**Delete:** 114-123, 126-131, 134-137 (the `reorder` tests).

**Change:** the `_add` helper (50-51) uses `batch = batch or P.new_batch(doc)`, because `add_page` now refuses unknown boxes.

**Add:**
- `test_new_doc_is_version_2`: a fresh manifest is version 2 with rev 0 and no boxes.
- `test_a_box_title_keeps_its_punctuation`: "제3화: 문이 열렸다!" is kept; newlines and control characters are folded.
- `test_a_page_added_to_an_earlier_box_lands_at_the_end_of_that_box`: the order is a1, a2, b1.
- `test_adding_to_an_unknown_or_legacy_box_is_refused`: 404 and 409; the doc is unchanged.
- `test_pages_follow_box_order_after_reordering_boxes`
- `test_built_boxes_keep_their_place`: moving the built prefix gives 409; moving the unbuilt tail works.
- `test_deleting_a_box_removes_its_pages_but_never_a_built_box`
- `test_move_within_a_box`
- `test_move_across_boxes_keeps_the_given_order_and_recounts`
- `test_move_to_the_same_spot_is_a_no_op_and_keeps_every_seam`: returns False and the user's seam is intact.
- `test_move_resets_exactly_the_seams_whose_neighbour_changed`: the old next page, the moved page and the new next page are reset (even user-set ones); all other seams are untouched.
- `test_move_refuses_unknown_pages_box_or_anchor`: 404, 404 and 400; nothing is moved.
- `test_move_never_renames_files`
- `test_deleting_a_page_resets_the_seam_after_it`
- `test_skipping_a_page_resets_the_seam_that_now_spans_it`
- `test_the_first_page_of_a_box_has_no_seam`
- `test_normalize_is_idempotent_and_adopts_orphans_deterministically`
- `test_v1_runs_become_boxes_in_first_appearance_order`: batches b1, b1, b2, b1 give 3 boxes; the third has a derived id and b1's label; an empty v1 batch is dropped.
- `test_v1_migration_is_stable_across_unsaved_loads`: two loads give the same box ids.
- `test_v1_built_pages_keep_their_chapter_in_chapter_order`
- `test_v1_page_that_missed_its_build_rejoins_its_chapter`: pages p4 (chapter 2), p5 (unbuilt), p6 (chapter 2) end up in one box with chapter 2.
- `test_v1_whole_novel_split_becomes_a_locked_legacy_box`: state is legacy with number None; the next unbuilt box is `chapters + 1`.
- `test_v1_seams_survive_only_where_the_neighbour_is_unchanged`
- `test_a_v2_manifest_is_not_migrated_again`
- `test_save_bumps_rev`
- `test_box_state_and_projected_number`: covers unread, reading, to-check and ready, and the number `chapter_count + position`.
- `test_a_built_box_turns_changed_on_any_content_change`: parametrized over text, reorder, page moved in or out, skip, join and label; a status-only change stays `built`.
- `test_blockers_name_every_page_that_holds_a_box_back`

### tests/test_ocr_build.py

**Delete:** 100-103 (separator), 169-174 (`test_reordered_pages_group_by_arrangement_not_upload_order`, whose premise is reversed), 179-184 (heading), 187-190 (single), 193-195 (unknown mode), 215-218 (pile append).

**Rewrite on `build_boxes`**, using a `boxes(*groups)` helper that builds a normalized v2 doc and sets `join_prev_id`:
- 108-111 becomes `test_skipped_pages_are_left_out_without_blocking`.
- 114-120 becomes `test_an_unchecked_page_holds_its_box_back_instead_of_vanishing`.
- 123-124 becomes `test_a_page_with_no_text_blocks_its_box`.
- 129-166: one chapter per box in box order; no heading needed; the label is the title with punctuation kept; the heading is the fallback title; sentence joins survive inside a box.
- 198-200 becomes `test_nothing_ready_builds_nothing`.
- 205-212 uses `existing = 12 chapters` and expects `added == [13]`.

**Add:**
- `test_box_membership_not_adjacency_decides_the_chapter`: interleaved pages b2, b1, b2 give 2 chapters.
- `test_include_all_accepts_unchecked_but_never_unread_pages`
- `test_building_stops_at_the_first_box_that_isnt_ready`
- `test_an_empty_or_all_skipped_box_blocks_later_boxes`
- `test_a_changed_built_box_is_rebuilt_in_place_from_its_current_pages`: the new order is used and `page_map` is updated.
- `test_a_page_moved_between_built_boxes_moves_its_text`
- `test_a_photo_never_feeds_two_chapters`: the source box isn't ready, so the target box waits.
- `test_unchanged_built_boxes_are_left_alone`
- `test_a_title_only_change_is_updated_without_a_retranslate`
- `test_undecided_seams_are_filled_by_the_free_rules_and_written_back`
- `test_an_unsure_seam_becomes_a_paragraph_with_a_warning`
- `test_a_heading_inside_a_box_is_warned_about`
- `test_start_over_renumbers_every_box_from_one`
- `test_legacy_boxes_are_never_rebuilt`
- `test_the_box_reads_built_right_after_its_build`: guards the consistency between the fill and the signature.

### tests/test_pages_api.py

**Rewrite:**
- 183-199: the reorder tests become `test_pages_can_be_moved_within_a_box` and `test_a_move_naming_a_missing_page_is_refused`.
- 202-207: loop over `delete`, `move`, `batches`, `batches/order`, `stitch` and `build` (preview), and assert "That page isn't in this novel" is not in the response.
- 212-238: the second page is uploaded with `batch=first["batch"]`. Otherwise the two pages are separate chapters with no seam, and the second test would pass without testing anything.
- 296-303: assert "needs checking" is in the response.
- 306-323: `added == [2]`.
- 392-402: assert "up to date" is in the response.
- 425-436: delete; replaced by `test_a_v1_whole_novel_split_stays_locked`.

**Add:**
- `test_uploading_into_an_earlier_box_lands_at_the_end_of_that_box`
- `test_uploading_into_an_unknown_box_is_refused_not_a_new_box`
- `test_a_duplicate_reports_its_box_and_chapter_without_a_batch_key`
- `test_creating_renaming_and_reordering_boxes`
- `test_a_built_box_cannot_be_moved_or_deleted`
- `test_deleting_a_box_removes_its_photos`
- `test_stitching_only_pairs_pages_inside_a_box`
- `test_stitching_keeps_model_decisions_and_moves_past_the_cap`: monkeypatch `A.Translator` and `A.ocr.stitch_boundaries`.
- `test_stitch_skips_a_seam_whose_neighbour_moved_during_the_model_call`
- `test_a_photo_added_to_a_built_chapter_joins_that_chapter`
- `test_reordering_a_built_chapter_reaches_the_text_and_photo_strip`
- `test_an_unchecked_page_holds_its_chapter_back` (end to end)
- `test_build_preview_writes_nothing`
- `test_pile_build_modes_are_refused`
- `test_chapter_photo_ids_skip_deleted_pages`
- `test_build_refuses_when_chapters_and_boxes_disagree` (409)
- `test_list_reports_chapter_count_box_numbers_and_rev`
- `test_reading_queues_pages_in_box_order`
- `test_a_build_is_not_interleaved_with_a_page_change`: a patched `cache_source` starts a thread that calls `mutate_pages`. The test asserts the thread is still blocked during the build and that both writes persist afterwards.

## 7. Frontend-facing contract (for consistency)

- **New api methods:** `createBatch`, `orderBatches(ids_all)`, `renameBatch`, `deleteBatch`, `movePages({ids, batch, before})`.
- **`buildChapters`:** `{include, append, force, preview}`.
- **Removed:** `reorderPages`.
- **Box row shape** and `chapter_count`/`rev`: as in §1.4.
- **Use `rev`** to drop stale polls.
- **Response fields:**
  - `added` is a list.
  - Duplicate uploads use `existing_number`.
  - Waiting entries carry `reasons`.
  - Show all of `warnings`.

### Critical Files for Implementation
- C:/Users/Mommy/Documents/night-reader/night-reader/server/pages.py
- C:/Users/Mommy/Documents/night-reader/night-reader/server/ocr_build.py
- C:/Users/Mommy/Documents/night-reader/night-reader/server/app.py
- C:/Users/Mommy/Documents/night-reader/night-reader/tests/test_pages_api.py
- C:/Users/Mommy/Documents/night-reader/night-reader/tests/test_ocr_build.py