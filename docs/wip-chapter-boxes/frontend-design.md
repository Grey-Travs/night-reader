# Frontend design: chapter boxes on the Pages tab

## 0. Changes I propose to the draft contract (the backend design has to adopt these)

1. **GET /pages batches: drop `page_ids` and `blockers`.**
   - The client works out each box's photos and their order from `pages[]`, which the server already keeps grouped by box in box order.
   - Per-box counts (to check, not read, and so on) come from page statuses on the client.
   - Keeping two sources of truth would mean every optimistic update has to patch both.
   - Keep `{id, label, at, count, chapter, number, state, legacy?, chapters?: [first,last]}` plus the top-level `chapter_count`.
   - The server must guarantee that every `page.batch` is a known box id.
2. **Every write returns the fresh summary under `pages`.** Stitch already does this (`PagesPage.jsx:236` uses `setRail(res.pages)`). It applies to move, box create/order/rename/delete, and pages/delete.
   - The client applies the response directly, which saves a GET per action over the phone link.
   - It is also what lets the sequence guard work ("latest write wins").
3. **POST /pages/build gains `dry_run`.** There is precedent in `api.js:177-180` (mergeSeriesGlossary `dry_run`) and `187-196` (postingPlan).
   - The dry run and the real build return the same shape:
     ```
     plan: [{batch, number, title, action: add|refresh|keep|wait|blocked, photos, reasons:[str]}]
     warnings
     added        (int, unchanged)
     refreshed    ([int], unchanged)
     chapters
     project      (real build only)
     ```
   - The dialog shows the server's own plan, so readiness rules are never re-implemented in JS.
4. **POST /pages/batches accepts optional `ids`**, moved into the new box in the given order, atomically. This powers "Split chapter here" and dropping a tile onto "New chapter".
   - The response is `{batch:{id,…}, pages}`.
   - If `after` would put an unbuilt box before a built one, refuse with 409. Do not clamp it: clamping would silently carry a split-off tail away from its chapter.
5. **Box order and box delete refuse rather than clamp.**
   - POST /pages/batches/order returns 400 for any order that moves a built or legacy box, or puts an unbuilt box before one.
   - POST /pages/batches/{id}/delete accepts optional `{expect_page_ids}` and returns 409 if they differ from the box's photos. That covers an upload landing after the confirm was shown.
6. **A seam's neighbour is the previous non-skipped page in the same box.** Stitch (`app.py:1617`) and assembly already ignore skipped pages. Toggling a page to or from `skipped` must also reset the seam of the page after it.
7. **Rename: don't use `safe_label`.**
   - `safe_label` (`pages.py:199-202`) is a filename sanitiser, and it strips `: , ! ? ' "` from what becomes the chapter title. Trim, collapse whitespace, strip control characters, cap at 120.
   - Put the label into `built_sig`, so renaming a built box shows "changed" and the next build updates the title. Today `_refresh_built_chapters` keeps the old title (`app.py:1693`).
8. **Upload duplicates.**
   - The duplicate response carries `batch` (the existing photo's box). The client must never adopt that as the drop's box (see §3).
   - An unknown box returns 404 and a legacy box returns 409. In both cases the upload stops with that file.
9. **Stitch response gains `left`**: unsure seams not yet sent to the model. The toast can then say "press again for the next ones".
10. **Remove POST /pages/reorder** (`app.py:1443-1449`). Nothing will call it, and a full permutation can break box grouping or move a built chapter's pages.
    - The frontend deletes `api.reorderPages`, `web/src/reorder.js` and `web/src/reorder.test.js`.
    - The lessons from `reorder.test.js` (off-by-one, permutation, stale ids) carry over into `pageBoxes.test.js`.
11. **Route order.** `/pages/batches/order` must be declared before `/pages/batches/{id}`, or "order" is taken as a box id. Also validate box ids against the 8-hex pattern.
12. **Legacy box** (a v1 `"*"` build):
    - Shape: `legacy:true, chapters:[1,N], chapter:null, number:null, state:'built'`.
    - It is fully locked: no upload into it, no moves in or out, no rename, no delete.
    - "Start over" turns it into ONE chapter, because a box is a chapter, and the start-over confirm says so. This is a deliberate simplification; there is no real v1 data.
13. **Narrowed: background drops.** The draft says "drop on the page background → new box". Instead, only explicit targets accept drops:
    - each box;
    - the header "Add chapter" button;
    - the dashed "New chapter" zone after the last box;
    - the empty state.

    Reason: a gap between boxes is a thin target, and a near miss would silently create an out-of-order chapter. Drops in a gap do nothing; a window-level `preventDefault` stops the browser opening the image, and a toast says where to drop.
14. **Seams in the UI.** `chapter` leaves the seam click-cycle, because inside a box it is only a paragraph break. When the server proposes it, the UI shows it as "heading here — split?". No backend change is needed.

## 1. File layout

- **`web/src/pageBoxes.js` (new, pure) and `web/src/pageBoxes.test.js` (new).** Placed beside `tasks.js`/`prefs.js`. Holds all ordering logic (§2).
- **`web/src/pages/PagesPage.jsx`** becomes the container, about 420 lines. It owns:
  - the outlet context, plus `chapters` (`ProjectLayout.jsx:406,426`);
  - the rail with the sequence guard, the poll, and the `?photo=` URL state;
  - the current page record, draft, `persist`, autosave and flush;
  - the upload queue and its banner (an inline `UploadStatus`);
  - every write, and the header actions.

  It renders `ChapterBoxes` or `PagePhotoView`, plus `BuildChaptersDialog`.
- **`web/src/components/ChapterBoxes.jsx` (new).** The overview: boxes, tiles, seam markers, the "+" tile, the "New chapter" zone, the empty state and all drag-and-drop. It takes props only and has no router hooks.
- **`web/src/components/PagePhotoView.jsx` (new).** Built from `PageViewer` (`PagesPage.jsx:501-526`) and `PageEditor` (`530-632`), plus the seam, move and split controls. `MIN_HANGUL` (`:28`) and `CONFIDENCE_CLS` (`:30`) move here.
- **`web/src/components/BuildChaptersDialog.jsx` (new).** Replaces `BuildDialog` (`:636-702`).
- **Reused and moved from PagesPage.jsx:**

  | Code | Lines | Where it goes |
  |---|---|---|
  | loadRail (gets the guard) | 59-69 | container |
  | poll | 76-85 | container |
  | fetch current page | 94-101 | container, keyed on `photoId` |
  | persist (keeps the has_text/chars recompute) | 111-139 | container |
  | onDraft | 141-149 | container |
  | flush effect | 156-161 | container, deps `[pid, photoId]` |
  | run / startPageWork | 201-219 | container |
  | readAll, verifyFlagged, stitch, rereadOne, verifyOne | 224-250 | container |
  | acceptAllGood (button moves to the header) | 252-260 | container |
  | removeSelected → `deletePhoto` | 262-276 | container |
  | setJoin | 278-287 | container |
  | JOIN_CHIP labels and hints | 18-24 | `SEAMS` in pageBoxes.js |

- **Deleted:**
  - auto-select (`:88-92`): the overview comes first now;
  - `step` (`:105-108`): stepping now stays within one chapter;
  - the upload loop (`:164-190`);
  - reorder / `dropOn` (`:290-318`);
  - `PageRail` and `JoinChip` (`:450-497`);
  - the mode select;
  - `window.confirm` (`:650`);
  - the unused `useMemo` import becomes used.

## 2. `pageBoxes.js` (pure; every move goes through it)

```js
export const NEW_BOX = '__new__'                        // upload target: a new chapter at the end
export const TILE_MIME = 'application/x-night-reader-page'
export const BUSY = new Set(['queued', 'ocr-running'])
export const SEAMS = { sentence:{icon:'→',label:'→ continues',cls:'pill-translated',hint:…}, paragraph:{icon:'¶',…'pill-muted'},
                       gap:{icon:'⚠',…'pill-review'}, chapter:{icon:'📖',label:'heading here',cls:'pill-english',…} }
sortFilesByName(files)            // Intl.Collator(undefined,{numeric:true,sensitivity:'base'}); tie → original index
groupBoxes(pages, batches)        // [{...batch, pages}] in batch order; pages whose box is unknown go into {id:null, orphan:true}
locate(boxes, pageId)             // {box, boxIndex, index} | null
seamPrev(boxPages, i)             // index of the previous non-skipped photo in the box, or -1 (no marker)
neighbourId(boxes, id, ±1)        // ← → within the box only
planPhotoMove(boxes, id, how)     // how: 'earlier'|'later'|{toBox}. Returns {ids,batch,before} | null
planDrop(boxes, draggedId, {boxId, overId, side})   // null = no-op or invalid
applyMove(rail, {ids,batch,before})                 // optimistic rail | null
planBoxMove(boxes, id, ±1)        // new id list | null. Built and legacy boxes never move, and nothing passes them
applyBoxOrder(rail, ids)          // reorder batches, regroup pages, re-project unbuilt numbers | null
planSplit(boxes, pageId)          // {after, ids} | null
nextSeam(kind)                    // sentence→paragraph→gap→sentence; '' and 'chapter' → sentence
boxCounts(box)                    // {total, unread, reading, check, failed, skipped, noText}
uploadReport(job, boxes)          // {toast, notice}
```

**Rules that matter:**

- **`planPhotoMove`**
  - `earlier` → `before = pages[i-1]`.
  - `later` → `before = pages[i+2] ?? null`.
  - `{toBox}`: if the target box comes earlier, the photo goes at its end; if later, at its start (`before = target.pages[0]?.id ?? null`). This matches the usual boundary mistake.
  - Returns null for the same box, a legacy box, or the first/last photo when that move is impossible.
- **`planDrop`**
  - `side:'after'` → `before = next photo in the box, or null`.
  - Returns null when `overId === draggedId`, when `before === draggedId`, or when the photo already sits directly before `before` in the same box.
  - Returns null if either box is legacy.
  - Dropping on a box with no `overId` puts the photo at the end.
- **`applyMove`**
  - Checks: every id exists, the target box exists, and `before` is in the target box and not itself moving. Otherwise null, and the caller sends the request without an optimistic update.
  - Removes the ids from wherever they are, inserts them in the given order, sets `page.batch`, and recounts `batches[].count`.
  - Resets `join_prev`/`join_prev_source`/`join_reason` to `''` and `join_glue` to `'space'` on every page whose `seamPrev` neighbour changed, and only those. This mirrors the server rule (§0.6).
- **`applyBoxOrder`**: unbuilt boxes get `number = rail.chapter_count + k + 1`.
- **`planSplit`**
  - Null on the box's first photo, on a legacy box, or when any built or legacy box comes after this one.
  - It is allowed on the last built box.
- **`uploadReport`**: see §3.5.

## 3. Container: `PagesPage.jsx`

### 3.1 Sequence guard (a stale poll can't undo a local change)

```js
const epoch = useRef(0), inflight = useRef(0), ticket = useRef(0), railRef = useRef(null)
const track = async (fn) => { inflight.current++; epoch.current++
  try { return await fn() } finally { inflight.current--; epoch.current++ } }
// loadRail: const mine = ++ticket.current, e0 = epoch.current, busy0 = inflight.current > 0
//   apply only if mine === ticket.current && !busy0 && epoch.current === e0   (a skipped response returns true)
// write responses: applyRail(res.pages) only if inflight.current === 0 after this one ends
//   (the last write to finish carries every write before it). On error: showError + loadRail().
```

- **Tracked writes:** move, box create/order/rename/delete, pages/delete, stitch, persist and setJoin.
- **Uploads are NOT tracked.** They make no optimistic change, and tracking them would discard every poll and every resync during a long drop.
- **`applyRail`** sets both state and `railRef` (the upload report needs the fresh boxes inside an async closure).
- **One poll change:** when `running` goes from true to false, run one final `loadRail({quiet:true})`, so the last page's result shows. Today it is missed.
- Otherwise the poll stays exactly as at `:76-85`, which keeps the existing tests valid.

### 3.2 URL state

`const [params, setParams] = useSearchParams(); const photoId = params.get('photo')`

- **Open a photo:** push, and set `openedHere.current = true` and `lastPhoto.current = id`.
- **← →, deleting the open photo, an unknown id:** replace (`{replace:true}`), so the browser's Back button goes to the boxes rather than through each photo.
- **"Back to chapters":** if `openedHere.current`, call `navigate(-1)`; otherwise drop the param with replace. Then run a quiet `loadRail`.
- Always build a fresh `URLSearchParams` inside the updater.
- **Scrolling:**
  - On entering the photo view, call `window.scrollTo(0,0)`. BrowserRouter has no scroll management.
  - On returning, the overview force-expands the box holding `lastPhoto` and calls `scrollIntoView({block:'center'})` plus `focus()` on `[data-page-id]`.

### 3.3 Photo record and editor (behaviour to preserve)

- **Fetching and saving**
  - `current` is fetched on `[pid, photoId]` (as `:94-101`, with zoom reset).
  - `persist` and `onDraft` stay as they are, with `photoId` in place of `selectedId`.
  - The flush effect is unchanged, which means stepping, Back and unmount all flush the pending save.
  - `persist` with a `status` field also triggers a quiet `loadRail`, so the box pill and the Build button update.
- **Refresh when reading finishes**
  - A ref `{id, status}` tracks the open photo's row status.
  - On a busy → idle transition for the same id, re-fetch `api.page` and set `current` and `draft`.
  - The textarea is `readOnly` while `row.status ∈ BUSY`, with the hint "Claude is reading this photo…". The server overwrites the text when it finishes, so typing then would be lost anyway.
- **Seams:** the seam control reads the join fields from the **rail row**, which polls and writes keep fresh. Text, verify, notes and hint come from `current`.
- **`deletePhoto`**
  - Confirm first.
  - Clear `pendingSave` and the timer, so a save is never sent for a page that no longer exists.
  - Then `track(deletePages)` → `applyRail`, and replace the URL with the next photo in the box, or the previous one, or the boxes view.

### 3.4 Upload queue (one chain; the target is fixed when the drop is queued)

```js
function enqueueUpload(fileList, target /* box id | NEW_BOX */) {
  const files = sortFilesByName([...(fileList || [])].filter((f) => f.size)); if (!files.length) return
  const job = { id: ++jobSeq.current, pid, gen: stopGen.current, target, batch: target === NEW_BOX ? '' : target,
                files, done: 0, added: 0, sameBox: 0, elsewhere: [], stoppedAt: null }
  setUploads((q) => [...q, view(job)])
  chain.current = chain.current.then(() => runUpload(job)).catch((e) => showError(e, { context: 'adding photos' }))
}
// runUpload: if job.gen !== stopGen.current → drop the job. For each remaining file (re-check gen first):
//   res = await api.uploadPage(job.pid, file, { batch: job.batch })
//   if (res.duplicate) res.batch && res.batch !== job.batch ? job.elsewhere.push({name, batch: res.batch, pageId: res.page?.id}) : job.sameBox++
//   else { job.added++; if (!job.batch) job.batch = res.batch }   // ONLY a real add sets the box
//   catch → job.stoppedAt = {index:i, name, error:e}; break
// then remove from uploads, await loadRail({quiet:true}), report = uploadReport(job, groupBoxes(railRef.current…))
```

**Why each piece is there:**

- **A duplicate's `batch` is another chapter.** Copying today's `batch = batch || res.batch` (`:175`) would send the rest of a new-chapter drop into an old chapter.
- **"Stop"** increments `stopGen`. Drops queued after Stop still run.
- **`pid`** is captured with the drop, so an upload still running after the user switches novels lands in the right one.
- **Ghost tiles** (file name plus "waiting"/"uploading") render at the end of the target box from `uploads`. A new-chapter drop shows a ghost "New chapter" box until its box id appears in the rail.

### 3.5 Upload messages (`uploadReport`)

- **Toast:** "8 photos added to Chapter 5 · 2 already here".
- **Persistent notice, duplicates elsewhere:** "IMG_0003.jpg is already in Chapter 4 (photo 9), so it wasn't added to Chapter 5." It lists up to 3 files, then "and N more".
- **Stopped notice** (`pill-review`): "Stopped at IMG_0004.jpg (photo 4 of 10): `<e.message>`. 3 photos went into Chapter 5; the 6 after it were not sent. Fix that photo, then add all 10 again with Chapter 5's + — the ones already there are skipped, so the order stays right."
  - Buttons: [Try the rest again] re-queues `files.slice(stoppedAt.index)` into `job.batch || job.target`; [Details] calls `showError(e)`; [Dismiss].
  - On a 404 the retry button becomes "Add them as a new chapter instead" (target NEW_BOX).
- **New-chapter drop with nothing new:** "All 10 photos are already in Chapter 5."
- **Labels** are resolved live from the box id, so reordering boxes mid-upload never shows a wrong number.

### 3.6 Writes and header

- `moveTo(move)`: `applyMove` (optimistic), then `track(api.movePages)`, then guarded apply.
- `moveBox(id, delta)`: `planBoxMove` and `applyBoxOrder`, then `api.reorderPageBatches`.
- `addBox()`: `api.createPageBatch(pid,{})`, apply, then scroll to and focus that box's + tile. The file picker is not opened automatically, because iOS loses the user gesture after an await.
- `renameBox`: optimistic label change.
- `deleteBox(box)`:
  - Disabled while an upload targets the box.
  - An empty box is deleted without asking. Otherwise confirm (danger): "Delete Chapter 6 and its 8 photos? The photos and the text read from them are removed. This can't be undone."
  - Sends `{expect_page_ids}`.
- `mergeBox(box)`: `movePages(all, prevBox, null)` then `deletePageBatch(id,{expect_page_ids:[]})`.
- `split(pageId)`: `createPageBatch(pid, planSplit(...))`, then toast "Chapter 6 now starts at this photo".
- `readBox(box)`: `startPageWork('read', () => api.readPages(pid, ids of new/failed), …)`.
- **Header buttons:** [Add chapter] [Read N pages] [Double-check N] [Accept N confident] [Work out the joins] [Build chapters →].
  - The existing labels are kept, so the existing tests still match.
  - The stitch toast adds "· N more unsure — press again" when `left > 0`.
- **Subtitle:** "5 chapters · 3 built · 42 photos · 30 good · 3 to check · 4 not read yet".
- **Modal:** `const closeBuild = useCallback(() => setShowBuild(false), [])`. An inline `onClose` makes Modal steal focus on every 2.5 s poll (`ui.jsx:129-134`).

## 4. Overview: `ChapterBoxes.jsx`

- **Box:** `<section aria-label="Chapter N" id={`box-${id}`} className="card p-3">`.
- **Box header** (`flex flex-wrap items-center gap-2`):
  - **Number:** `font-reading text-lg` "Chapter {number}". Unbuilt boxes add `text-hint` "· new".
  - **Title**, as inline text or an input:
    - `label`; otherwise for a built box the chapter title from `chapters`; otherwise for an unbuilt box the first photo's `heading` in `text-hint`, captioned "from the first photo".
    - Click the title or ✎ to edit. Enter or blur saves, Escape cancels, `maxLength=120`.
    - Placeholder: "Add a title (optional) — becomes the chapter title".
  - **State pill:**

    | `state` | Label | Class |
    |---|---|---|
    | `empty` | No photos yet | `pill-muted` |
    | `unread` | {n} not read | `pill-queued` |
    | `reading` | Reading… | `pill-translating animate-pulse` |
    | `to-check` | {n} to check | `pill-review` |
    | `ready` | Ready to build | `pill-translated` |
    | `built` | the chapter's translation status (`STATUS_STYLES`/`STATUS_LABEL`, `ui.jsx:6-26`); `pending` shows as "Not translated yet" in `pill-muted` | — |
    | `changed` | Changed — build to update | `pill-english` |
    | `changed-waiting` | Changed — {n} to check | `pill-review` |

    The counts come from `boxCounts`.
  - **"{n} photos"** comes from `box.pages.length`.
  - **"Read {n}"** (`btn-quiet text-xs`) shows when the box has unread photos.
  - **Icon buttons**, each with `aria-label` and `tap`:
    - "Move Chapter N up" / "Move Chapter N down": disabled via `planBoxMove`, with a title such as "Built chapters keep their place".
    - "Rename Chapter N".
    - "Join to Chapter N-1": unbuilt boxes only.
    - "Delete Chapter N": unbuilt boxes only.
    - "Read chapter N →": built boxes; calls `onOpenChapter`.
    - "Show/Hide photos" toggle.
- **Collapsing:**
  - Rule: `open = expanded[id] ?? (state !== 'built' && !legacy) || id === focusBoxId`.
  - `expanded` lives in the container, which stays mounted across the photo view, and resets on pid change. No localStorage: the default rule already gives the right view for long novels.
  - A collapsed box renders no tiles, so no images are fetched.
- **Tiles** (`ul.flex.flex-wrap.gap-x-1.gap-y-3`). Each `li` holds [seam marker][tile], so a marker wraps together with the photo it belongs to.
  - The tile is `<div role="button" tabIndex={0} draggable data-page-id aria-label="Photo {pos} of Chapter {n}: {status}">`, and Enter/Space opens it.
    - Do NOT use `<button draggable>` (as at `:456`): Firefox does not start drags from buttons.
  - Width `w-28`, with `<img draggable={false} loading="lazy" decoding="async" className="aspect-[3/4] w-full rounded-card object-cover border border-line" style={{background:'var(--b-muted-bg)'}}>`.
  - The position is overlaid as `absolute left-1 top-1 pill pill-muted !px-1.5 !py-0 text-[11px] tabular-nums`. Below the image: `PageBadge`, then the name truncated in `text-[11px] text-hint`.
  - Skipped photos use `opacity-60` and have no seam.
- **Seam marker:**
  - A 16px column with a round `pill ${cls}` icon.
  - Its title is the hint plus `join_reason`, plus "(photo 2 is skipped)" when applicable.
  - Undecided seams (`join_prev===''`) show a dashed muted "·" titled "Not worked out yet".
  - **Click:**
    - Fine pointer: cycle with `nextSeam`.
    - `(pointer: coarse)`, or a `chapter` seam: open that photo instead (its seam and split controls are there). This avoids 44px chips between tiles and accidental cycling while scrolling.
- **"+" tile:** `w-28 aspect-[3/4] rounded-card border border-dashed border-line text-muted` "＋ Add photos", `aria-label="Add photos to Chapter N"`. It calls `onAddPhotos(id)`, and the container sets `pickTarget.current = id` before `fileRef.current.click()`. Hidden on legacy boxes.
- **"New chapter" zone** after the last box: a dashed full-width "＋ Add chapter — or drop photos here to start a new one" (click → `addBox`).
- **Empty state:** keeps the copy and the iPhone note from `:388-403`. Text: "Each chapter gets its own box, in reading order. Drop chapter 1's photos here, or [Choose photos]", plus "Add an empty chapter".
- **Legacy box:**
  - Header "Chapters 1–N · built from one pile before chapter boxes" with 🔒.
  - Collapsed by default, tiles not draggable, no "+" tile.

### Drag and drop

- **Telling drags apart:** `const types = (e) => Array.from(e.dataTransfer?.types || [])`.
  - A file drag has `types.includes('Files')`.
  - A tile drag has `includes(TILE_MIME)` **and** `dragRef.current`.
- **Tile:**
  - `onDragStart`: `e.dataTransfer.setData(TILE_MIME, id)` (Firefox needs data set) and `effectAllowed='move'`; set `dragRef.current = id`.
  - `onDragEnd` always clears `dragRef` and the drop hint. This fixes ordering risk #11.
  - `onDragOver`/`onDrop` handle tile drags only, and call `stopPropagation`. File drags bubble up to the box.
  - The side comes from the **drop** event itself: `clientX < rect.left + rect.width/2 ? 'before' : 'after'`.
  - The hint is a 3px `var(--accent)` bar on the left or right edge. It is only set when it changes.
- **Box section:**
  - A file drop calls `onDropFiles(files, box.id)`, which appends at the end.
  - A tile drop calls `planDrop(…, {boxId})`, which moves to the end.
  - `dragleave` only clears when `!e.currentTarget.contains(e.relatedTarget)`.
  - Legacy boxes accept nothing.
- **"Add chapter" button and the "New chapter" zone:**
  - Files → `NEW_BOX`.
  - A tile → `createPageBatch({ids:[id]})`.
- **`useFileDragActive()`:** a window enter/leave counter, reset on drop. While files are being dragged, every box shows a dashed accent outline and "Drop to add to Chapter N (at the end)", and the Add-chapter button reads "Drop here for a new chapter".
- **Window guard:** `dragover` and `drop` listeners call `preventDefault()` for file drags. A drop that misses every target then does nothing instead of navigating to the image, and toasts "Drop onto a chapter box, or onto New chapter".

## 5. Photo view: `PagePhotoView.jsx`

- **Top bar:**
  - [← Back to chapters]
  - "Chapter 5 · {title} — photo 3 of 8"
  - [◀ Previous photo] [Next photo ▶], disabled at the chapter's ends
  - optional "Next to check →", which jumps to the next needs-check photo in box order.
- **Layout:**
  - `grid gap-4 lg:grid-cols-[minmax(0,1fr)_26rem]`.
  - Photo card on the left (`lg:sticky lg:top-4`), with zoom −/%/+ and a `max-h-[75vh] overflow-auto` frame (from `:501-526`).
  - Editor on the right (from `:530-632`, with the textarea at `min-h-[50vh]`).
  - On phones it stacks: photo (`max-h-[55vh]`), then text.
- **Kept exactly from the current editor:**
  - status and confidence pills, notes;
  - verify issues with Apply (`:537-540`, `:559-576`);
  - character count and % Korean;
  - the low-Korean warning;
  - Re-read and Check, both through `startPageWork`/`submitTask`;
  - Looks good, Skip;
  - Delete (`btn-ghost`);
  - the hint input with `key={page.id}`.

  The new addition is `readOnly` while the photo is busy. "Accept all confident" moves to the header.
- **Seam section** ("Joins to photo 2"):
  - A segmented control in the style of `SeriesDetailPage.jsx:330-341`: [→ Sentence continues] [¶ New paragraph] [⚠ Page missing?], plus the `join_reason`.
  - First photo of a box: "First photo of the chapter — nothing to join to."
  - A `chapter` seam: "Claude saw a chapter heading on this photo", with Split highlighted.
- **Move section:**
  - [◀ Earlier] [Later ▶]
  - `<select aria-label="Move to another chapter">` with options "Chapter 4 · title (at the end)" for earlier boxes and "Chapter 6 · title (at the start)" for later boxes; legacy and orphan boxes are excluded.
  - [Split chapter here], disabled with a reason when `planSplit` returns null.
  - After a move the view stays on the photo, so ← → now step through its new box.
  - Legacy photos hide this section and say that edits don't reach the chapters built from them.
- **Keys:** a window `keydown` listener.
  - Ignored when: `defaultPrevented`; any of alt/ctrl/meta is held (Alt+← is browser Back); the target is inside `input, textarea, select, [contenteditable]`; or a `[aria-modal="true"]` dialog is open.
  - ArrowLeft/ArrowRight step to the neighbour. The pattern follows `ChapterReader.jsx:286-306`.
- **Unknown or deleted photo** once the rail is loaded: "That photo isn't in this novel any more" + Back.

## 6. `BuildChaptersDialog.jsx`

- **On open, and when `include` changes:** call `api.buildChapters(pid,{include, append:true, dry_run:true})` behind an `alive` guard; show a skeleton while it loads.
- **The plan, in box order:**
  - **Will be added:** Chapter N · title — k photos.
  - **Will be updated:** "Chapter 3 — photos changed since it was built". If that chapter's status (from `chapters`) is translated, validated or needs-review, add: "its English was made from the old text — translate it again afterwards".
  - **Waiting:** Chapter 6 — reasons.
  - **Blocked:** "Chapters 7–9 wait for chapter 6, so numbering stays in order."
  - Unchanged built chapters collapse into one line: "Chapters 1–4 built, unchanged".
- **Warnings:** all of them, in `pill-review`.
- **Controls:**
  - A checkbox, reset to `approved` each time the dialog opens: "Also use photos marked 'Needs a check'" (`include=all`).
  - Primary "Build", disabled with "Nothing is ready yet" when the plan has no add or refresh.
- **Start over** sits under `<details><summary>Start over…</summary>`:
  - Explanatory copy, which also says a legacy pile becomes one chapter.
  - A `btn-ghost` [Start over] → `useConfirm` (danger) → build with `append:false`.
  - If the server answers 400 matching `/already translated|renumber/`, a second `useConfirm` shows `e.message` → build with `force:true`. This replaces `window.confirm` (`:650`).
- **After a real build:**
  - Call `setProjectMeta(res.project)`, `reload(true)`, `loadRail()`.
  - With no warnings and no waiting boxes: close and toast "2 chapters added · chapter 3 updated".
  - Otherwise the dialog switches to a result view: built / updated / still waiting / **all** warnings, with [Done].

## 7. Other files

- **`api.js`:**
  - Add:
    - `createPageBatch(pid, body)` → POST `/pages/batches`
    - `reorderPageBatches(pid, ids)` → `/pages/batches/order`
    - `renamePageBatch(pid, id, label)` → `/pages/batches/${id}`
    - `deletePageBatch(pid, id, body)` → `/pages/batches/${id}/delete`
    - `movePages(pid, {ids, batch, before=null})` → `/pages/move`
  - Rewrite the comment at `:50-53`. A box is a chapter; uploads go INTO a box by id; an empty id starts a new box at the end; an unknown id is refused.
  - Delete `reorderPages`.
- **`guide.js:40-54`:**
  - Instruction: put each chapter's photos in its own box.
  - Flow: `['Chapter boxes','Claude reads them','You check the text','Build chapters','Translate']`.
  - `how` lines:
    - Add chapter → one box per chapter, in reading order.
    - "+" or drop adds photos by file name (IMG_9 before IMG_10).
    - The number on a photo is its place; drag it, or open it and use Earlier / Later / Move to chapter (works on a phone).
    - If an upload stops, fix the named photo and add the same photos again to the same box.
    - Read / check / Work out the joins (now "inside a chapter").
    - The "page missing?" marker.
    - Build: each box becomes one chapter, in box order. A box that isn't ready waits, and so do the boxes after it.
    - Once built, a chapter's place is fixed, but you can still fix its photos and text and build again.
    - The iPhone note.
- **`web/src/test/setup.js`:** add no-op `Element.prototype.scrollIntoView` and `window.scrollTo`, with a "why" comment. jsdom lacks the first and only logs "not implemented" for the second.

## 8. Tests

**`PagesPage.test.jsx`: fixture and mock changes**

- Router mock: `vi.mock('react-router-dom', async (actual) => ({ ...(await actual()), useOutletContext: () => outletContext() }))`, the partial-mock pattern from `App.test.jsx:17-22`. Render inside `<MemoryRouter initialEntries={[`/novel/${PID}/pages${at}`]}>`.
- API mock:
  - Rename `uploadPages` → `uploadPage`.
  - Add `createPageBatch`, `reorderPageBatches`, `renamePageBatch`, `deletePageBatch`, `movePages`.
  - Remove `reorderPages`.
- Confirm becomes a shared `const confirm = vi.fn(async () => true)`.
- Outlet context gets `chapters` and keeps `showError`.
- Fixtures:
  - `box(over)` → `{id:'b1',label:'',at,count,chapter:null,number:1,state:'unread'}`.
  - `railPayload(pages,{batches})` adds `chapter_count:0`.
  - `api.page.mockImplementation((_, id) => row by id + text:'')`.
  - Helpers: `file(name)` (non-zero size), `deferred()`.
  - Assert upload order with `api.uploadPage.mock.calls.map(c => c[1].name)`.
  - Stub `getBoundingClientRect` for the before/after side.
  - `dataTransfer` is a plain object `{types, files, setData: vi.fn(), getData}`; RTL assigns it as-is in jsdom.
- The existing render, submitTask and poll tests keep working ("Read 2 pages" label unchanged).

**New tests in `PagesPage.test.jsx`**

| # | Test | Asserts |
|---|---|---|
| 1 | shows one box per chapter, in order, with its photos numbered from 1 | regions "Chapter 1"/"Chapter 2"; tile labels "Photo 1…" within each |
| 2 | shows the empty state when the novel has no chapters yet | the "Drop chapter 1's photos here" copy |
| 3 | Add chapter creates a box | `createPageBatch(PID,{})`; region "Chapter 2" appears from the response |
| 4 | built chapters cannot be moved, joined or deleted | no Move/Delete buttons on a `chapter:1` box |
| 5 | moving a box down sends the whole new order | `reorderPageBatches(PID,['b1','b3','b2'])` |
| 6 | deleting a box asks first and sends the photos it expects to delete | `confirm` called; `deletePageBatch(PID,'b2',{expect_page_ids:[…]})` |
| 7 | uploads picked files in natural name order into the box whose + was clicked | names IMG_1, IMG_9, IMG_10; every call `{batch:'b2'}` |
| 8 | a new-chapter drop puts every photo in the box its first photo created | 1st call `batch:''`, then `'b9'` |
| 9 | a duplicate never redirects the rest of a drop into another chapter | a duplicate with `batch:'b1'` → next call still `''`; notice names "Chapter 1" |
| 10 | a second drop waits until the first has finished | deferreds; B's first call only after all of A's resolve |
| 11 | stops at the first failed photo, says where, and retries the rest into the same box | no 3rd call; notice "Stopped at IMG_2.jpg (photo 2 of 3)"; "Try the rest again" → IMG_2, IMG_3 into `b1` |
| 12 | files dropped on a box upload even after an abandoned tile drag | dragStart, no dragEnd, then a Files drop → `uploadPage`; `movePages` not called |
| 13 | dragging a tile onto another tile moves it there | `setData(TILE_MIME, id)`; `movePages(PID,{ids:['aaaaaaaa'],batch:'b1',before:null})` |
| 14 | dropping a tile back where it was sends nothing | `movePages` not called |
| 15 | Earlier / Later move within the chapter | `before:'aaaaaaaa'`; `before:null` |
| 16 | Move to chapter: end of an earlier chapter, start of a later one | the right `batch`/`before` for each |
| 17 | Split chapter here starts a new chapter from this photo | `createPageBatch(PID,{after:'b1',ids:['bbbbbbbb','cccccccc']})` |
| 18 | a poll that answers after a local move does not undo it | fake timers; `movePages` pending; poll returns the old order; tile order unchanged |
| 19 | clicking a photo opens it beside its text, and Back returns | `api.page(id)`; textarea shown; "Back to chapters" → boxes again |
| 20 | arrows step within the chapter only, and not while typing | ArrowRight on the last photo doesn't open box 2; keyDown on the textarea is ignored |
| 21 | a pending edit is saved, not dropped, when stepping to the next photo | `savePage(PID,'aaaaaaaa',{text})` |
| 22 | shows the text as soon as Claude finishes reading the open photo | poll flips queued → needs-check; `api.page` re-called; textarea shows the text |
| 23 | the text box is read-only while the photo is being read | textarea is `readOnly` |
| 24 | the build dialog shows each chapter's plan and why the rest wait | 1st call `{dry_run:true}`; reason text shown |
| 25 | shows every warning the build returns instead of closing | both warning strings plus Done |
| 26 | start over asks first, then forces only after a second yes | the call sequence `append:false`, then `force:true` |
| 27 | refreshes the boxes once more when the job finishes | rerender with `running` true→false → `api.pages` called again |

**New `pageBoxes.test.js`**, opening with a "why" comment that carries over the reorder lessons:

- **sortFilesByName:** IMG_9 before IMG_10; identical names keep selection order; case-insensitive; returns a permutation.
- **groupBoxes:** box order with page order inside; keeps an empty box; an unknown box id becomes an orphan box, never hidden.
- **planPhotoMove:** earlier and later; null at the ends; earlier chapter → end, later chapter → start.
- **planDrop:** left half → before, right half → after; dropping on itself or its own gap → null; legacy → null.
- **applyMove:**
  - within a box;
  - across boxes, updating `page.batch` and both counts;
  - several ids keep the given order;
  - resets exactly the seams whose previous non-skipped neighbour changed (the moved photo, and the photo after the gap it left);
  - null for an unknown page, unknown box, or a `before` that is itself moving;
  - an exhaustive permutation-and-grouping check over every page and position.
- **planBoxMove / applyBoxOrder:** moves only inside the unbuilt tail; re-projects numbers.
- **planSplit:** null on the first photo or when a built box follows; allowed on the last built box.
- **uploadReport:** counts; duplicates elsewhere with the chapter number and photo position; the exact stop and retry sentence.

## 9. Build order

1. `pageBoxes.js` and its tests.
2. `api.js`.
3. `PagePhotoView` and the `?photo=` wiring (moves the existing editor, so the current behaviour is easy to check).
4. `ChapterBoxes`, including drag-and-drop.
5. The upload queue.
6. `BuildChaptersDialog`.
7. `guide.js` and `setup.js`; remove `reorder.js` once `/pages/reorder` is gone.
8. Tests throughout.

The frontend needs these backend items in place first: §0 items 1-5, 8 and 12.

**Left out on purpose:**
- multi-select bulk moves;
- client-side hashing to detect duplicates before upload (`crypto.subtle` doesn't exist over the plain-http tailnet address);
- per-file rail patching during an upload (ghost tiles instead);
- localStorage for collapse state;
- uploading at a chosen position (photos are appended, then moved).

### Critical Files for Implementation
- C:/Users/Mommy/Documents/night-reader/night-reader/web/src/pages/PagesPage.jsx
- C:/Users/Mommy/Documents/night-reader/night-reader/web/src/pageBoxes.js (new; tests in web/src/pageBoxes.test.js)
- C:/Users/Mommy/Documents/night-reader/night-reader/web/src/components/ChapterBoxes.jsx (new), C:/Users/Mommy/Documents/night-reader/night-reader/web/src/components/PagePhotoView.jsx (new), C:/Users/Mommy/Documents/night-reader/night-reader/web/src/components/BuildChaptersDialog.jsx (new)
- C:/Users/Mommy/Documents/night-reader/night-reader/web/src/pages/PagesPage.test.jsx
- C:/Users/Mommy/Documents/night-reader/night-reader/web/src/api.js (also web/src/guide.js:40-54, web/src/test/setup.js)