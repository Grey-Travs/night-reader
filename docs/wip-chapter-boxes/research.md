# Research: the Pages tab before chapter boxes

## Frontend map

# Pages tab redesign (chapter boxes): frontend research report

Repo root: `C:/Users/Mommy/Documents/night-reader/night-reader/`. Every path below is under it.

## 0. Key findings

1. **No API exists to change which chapter (batch) a photo belongs to.**
   - A batch can only be created by an upload that sends no batch id, or an unknown one (`server/app.py:1414-1415`).
   - A batch's `label` can only be set on that first upload (`?label=`). It becomes the chapter title (`server/ocr_build.py:106-113`).
   - There is nothing to create an empty batch, rename one, delete one, or move a page between batches. `PageUpdate` has no `batch` field (`server/app.py:1816-1822`).
   - Empty batches are never removed. `delete_pages` only recounts them (`server/pages.py:288-289`).
2. **A new upload always goes to the END of the list** (`server/pages.py:265`). Batch builds group pages by contiguous runs (`_batch_runs`, `server/ocr_build.py:89-103`; pinned by `tests/test_ocr_build.py:169-174`). So adding a photo to chapter 2 while chapter 3 exists puts it after chapter 3, and it becomes a separate chapter. A "+ tile inside a chapter box" needs a server insert, or an upload followed by a full reorder.
3. **Files are never sorted before upload.** `PagesPage.jsx:165` does `Array.from(files || []).filter((f) => f.size)` and nothing else. The order is whatever the browser/OS hands over (picker or drag-drop), which is not guaranteed to be filename or capture order. Nothing in `web/src` sorts by `name` or `lastModified`.
4. **Two uploads can run at the same time.** "Add photos" (`PagesPage.jsx:339`) and the drop zones (`388-392`, `405-410`) stay enabled while `uploading` is set. Two loops then interleave arrivals and batches on the server, which splits chapters apart.
5. **A duplicate photo stays where it was.** The upload response for a duplicate is `{page, duplicate: true, total}` with no `batch` (`server/app.py:1405-1409`). The page keeps its old batch and position; the client only counts it as "already here".
6. **Upload is never tested.** The test mock defines `uploadPages` (plural, `PagesPage.test.jsx:32`), but the component calls `api.uploadPage` (`PagesPage.jsx:174`; `api.js:58`).
7. **The UI never shows batches.** The only mention is the BuildDialog option "Each batch of photos I added is one chapter" (`PagesPage.jsx:667`) and the guide text (`guide.js:46`). Nothing in the frontend reads `rail.batches` or `page.batch`.

## 1. `web/src/pages/PagesPage.jsx` (703 lines)

**Imports, constants and state**
- Line 1: imports `useCallback, useEffect, useMemo, useRef, useState`. `useMemo` is unused.
- Lines 2-7: `useOutletContext`, `api`, `reorderIds`, `useToast`, `useConfirm`, and `{ Modal, PageBadge, SkeletonRows }`.
- Lines 18-24: `JOIN_CHIP` with `sentence`/`paragraph`/`chapter`/`gap` (label, pill class, hint), and `JOIN_ORDER`, which is the click-to-cycle order.
- Line 28: `MIN_HANGUL = 0.15`. Line 30: `CONFIDENCE_CLS`.
- Line 33: `const { pid, running, submitTask, showError, reload, setProjectMeta } = useOutletContext()`.
- Lines 37-50 (state): `rail` (summary payload), `railError`, `selectedId`, `current` (full page record), `draft`, `busy` (string key), `uploading` (`{done,total}`), `zoom`, `showBuild`, `fileRef`, `saveTimer`, `pendingSave`.

**Loading and polling**
- Lines 59-69: `loadRail({quiet})`. It replaces the whole rail with `setRail(await api.pages(pid))`. A quiet failure only sets `railError` and does not open the error dialog.
- Line 71: initial load.
- Lines 76-85: a 2.5s poll that runs only while `running` is true. It stops after 3 failures in a row.
- Lines 88-92: once, auto-selects the first `needs-check` page, or the first page.
- Lines 94-101: fetches the selected page with `api.page(pid, selectedId)`. Its dependencies are only `[pid, selectedId]`, so the poll does not refresh the editor. If OCR finishes for the page on screen, its text doesn't appear until you select it again.
- Lines 105-108: `step(±1)` walks the global page order, crossing batch boundaries.

**Saving**
- Lines 111-139: `persist(fields, {quiet, pageId})`. It patches the rail row optimistically and recomputes `has_text` and `chars`, because those exist only on rail rows.
- Lines 141-149: 700ms autosave debounce.
- Lines 156-161: on unmount or page change, the pending save is flushed rather than dropped.

**Upload (lines 164-190)**
```js
let batch = ''
for (const [i, file] of list.entries()) {
  const res = await api.uploadPage(pid, file, { batch, label: '' })
  batch = batch || res.batch || ''
  ...  catch → showError(...); break
  setUploading({ done: i + 1, total: list.length })
}
setUploading(null); await loadRail(); toast('N pages added · M already here')
```
- Uploads are strictly one at a time, one batch per drop. The first error stops the loop and leaves a partial batch.
- Lines 192-198: `dragOver` state. `onDrop` calls `upload(e.dataTransfer?.files)`.

**Jobs and actions**
- Lines 201-204: `run(what, fn, context)` wrapper, which sets `busy`.
- Lines 213-219: `startPageWork` goes through `submitTask`. This is required; there is a regression test for it.
- Lines 224-250: `readAll`, `verifyFlagged`, `stitch` (calls `api.stitchPages(pid,{use_model:true})` and does `setRail(res.pages)`), `rereadOne`, `verifyOne`.
- Lines 252-260: `acceptAllGood`, a loop of `savePage` calls one at a time.
- Lines 262-276: `removeSelected`, using `useConfirm` with `danger: true`.
- Lines 278-287: `setJoin`, with an optimistic `join_prev_source: 'user'`.

**Reorder (lines 290-318)**
- The dragged page id is held in a ref, `dragId`.
- `dropOn` builds the new order from the current `pages` with `reorderIds`.
- The optimistic update only happens if the page count hasn't changed (`rows.length === r.pages.length`).
- It then sends the full list with `api.reorderPages(pid, next)`. On failure it calls `showError` and `loadRail()`.

**Render (lines 322-446)**
- Header with `<h1 className="font-reading text-2xl font-medium">Pages</h1>` and a counts line (324-333).
- Hidden `<input type=file accept="image/jpeg,image/png,image/webp" multiple hidden>` with `onChange={(e) => { upload(e.target.files); e.target.value = '' }}` (335-338).
- Buttons (339-361):
  - "Add photos" (`btn btn-quiet px-3 py-1.5 text-sm`)
  - "Read N pages" (`btn-primary`)
  - "Double-check N"
  - "Work out the joins"
  - "Build chapters →" (`btn-primary`)
  - The action buttons are `disabled={!!busy || running}`. "Add photos" is never disabled.
- Upload banner: `rounded-card px-3 py-2 text-sm pill-queued` (365-369).
- Error state with retry (371-384), `SkeletonRows rows={8}` (385-386).
- Empty drop zone (387-403): `rounded-card border border-dashed p-10 text-center`, using `border-line-strong` while dragging over and `border-line` otherwise.
- Populated view: a 3-column grid `grid gap-4 lg:grid-cols-[15rem_minmax(0,1fr)_22rem]`, which is also a drop target (405-427).
- `BuildDialog`'s `onBuilt` calls `setProjectMeta(res.project)`, `reload(true)`, `loadRail()`, then a toast (430-443).

**Sub-components**
- **PageRail (450-478):** a `card max-h-[70vh] overflow-y-auto p-2` list.
  - A `JoinChip` sits between rows.
  - Each row is a `<button draggable>` with `onDragStart`, which sets the ref but not `dataTransfer.setData`. Firefox normally needs `setData` before a drag starts.
  - `onDrop` calls `e.stopPropagation()` and then `onDropOn`.
  - Classes: `rowhover ... rounded-card p-2`, plus `sunken` when selected.
  - Thumbnail: `<img src={api.pageImageUrl(pid,id)} loading="lazy" decoding="async" className="h-12 w-9 ... object-cover" style={{background:'var(--b-muted-bg)'}}>`.
  - Label: `page.name || \`Page ${page.seq}\`` plus `PageBadge`. Note that `seq` is the upload sequence number, not the page's position.
- **JoinChip (480-497):** a hairline, a `pill ${cls} !px-1.5 !py-0 text-[11px]` button that cycles the kind, then another hairline.
- **PageViewer (501-526):** prev/next with an "i / N" counter, zoom −/%/+, and the image `scale(zoom)`.
- **PageEditor (530-632):** status and confidence pills, notes, verify issues with "Apply", a `textarea.input.font-korean`, character count and % Korean, a low-Korean warning, action buttons (`btn btn-quiet px-2 py-1 text-xs`; Delete is `btn-ghost`), and a hint input with `key={page.id}` so it remounts per page.
- **BuildDialog (636-702):** a `Modal` with three settings.
  - `mode`: `batch`, `heading`, `separator` or `single`; default `batch`.
  - `include`: `approved` or `all`.
  - `append`: checkbox, default true.
  - The forced-rebuild confirmation uses `window.confirm` (650), not `useConfirm`.

## 2. `web/src/pages/PagesPage.test.jsx` conventions

- **Framework:** Vitest with `@testing-library/react`. Interactions use `fireEvent`, because `@testing-library/user-event` is not installed (`web/package.json`). jest-dom matchers come from `web/src/test/setup.js:1`.
- **Top-of-file comment:** a narrative "why" block describing the bug the tests guard (lines 7-16). Other test files follow the same pattern.
- **Router mock (18-19):** `vi.mock('react-router-dom', () => ({ useOutletContext: () => outletContext() }))`. Only `useOutletContext` is mocked, so importing `Link` or `useNavigate` in PagesPage would break the test unless the mock is extended. SeriesDetailPage.test.jsx:18-21 shows how to mock `Link`.
- **API mock (21-35):** a whitelist of `vi.fn()`s, so every new api method must be added. `pageImageUrl` is a real function. `uploadPages` is the wrong name (see finding 6).
- **Toast and confirm (37-39):** `toast` is one shared `vi.fn()` defined at module level. `useConfirm` always resolves `true`.
- **Fixtures:**
  - `STATUSES` (44-45).
  - `page(over)` (47-57) mirrors a `summary()` row (`has_text`, `chars`, `batch: 'b1'`, `seq`, ...).
  - `railPayload(pages, over)` (59-71) builds counts with every status set to 0. Its `batches` entry is `{id:'b1', label:'', added_at}`, but the server shape is `{id, label, at, count}` (`server/pages.py:219`).
- **`renderPages({ submitTask, running, pages })` (73-87):**
  - Default `submitTask` is `vi.fn(async (call) => call())`.
  - Outlet context: `pid: 'abcdef012345'`, `showError`, `reload` and `setProjectMeta` as `vi.fn()`.
  - Default rows are two pages, `aaaaaaaa` and `bbbbbbbb`.
  - `api.page` resolves to `list[0]`.
  - Returns `{ submit, list, ...render() }`.
- **`beforeEach` (89-93):** `vi.clearAllMocks()`, then `readPages` and `verifyPages` resolve with job payloads (`{job_id, kind, current, pending}`).
- **Assertions:** buttons are found by role and name, e.g. `findByRole('button', { name: /Read 2 pages/i })`, followed by `waitFor(() => expect(api.x).toHaveBeenCalledWith(...))`.
- **Fake timers (157-179):**
  ```js
  vi.useFakeTimers(); try { …; await vi.waitFor(...); await vi.advanceTimersByTimeAsync(2600) } finally { vi.useRealTimers() }
  ```
- **Coverage:** the tests cover rendering, `submitTask` wiring for read and verify, refresh-and-toast after a job starts, no toast when `submitTask` returns null, and the poll gate. Upload, drag-reorder, BuildDialog, joins and delete have no tests.
- **`web/src/test/setup.js`:**
  - After each test: `cleanup()` and `vi.restoreAllMocks()`.
  - Polyfills: `matchMedia`, `requestAnimationFrame`, `EventSource`.
  - jsdom has no `DataTransfer`. Drop tests would need `fireEvent.drop(el, { dataTransfer: { files: [...] } })`, and file input tests `fireEvent.change(input, { target: { files: [...] } })`. The `File`s need a non-zero size because of the `f.size` filter.
- **Config:**
  - `web/vite.config.js` test block: `environment: 'jsdom'`, `globals: true`, `setupFiles`, `include: ['src/**/*.test.{js,jsx}']`, `pool: 'threads'`, `fileParallelism: false`.
  - Run with `npm test` (`vitest run`).
  - No existing frontend test uses drop, drag or file upload.
- **Backend tests:** `tests/test_pages_api.py` uses a `client` fixture with a monkeypatched `PROJECTS_DIR`, plus `_upload(client, pid, data=JPEG, **params)` (63-65), `_set` (68-71), and the byte constants `JPEG`/`JPEG2`/`PNG`/`HEIC`. Batch tests are at lines 244-271. `tests/test_ocr_build.py` has `page(..., batch=)` and `doc(pages, batches)` helpers (36-45).

## 3. Other uploaders

- **`web/src/pages/PhonePage.jsx` uploads nothing.** It is the posting-run console (`usePostingConsole`) and is mounted outside AppShell (`App.jsx:98`). Its test mocks only the posting APIs (`PhonePage.test.jsx:19-29`). Its file-local mini-components `Tile` (263-271) and `Num` (273-290) are an example of the inline sub-component style.
- **`PagesPage.jsx:174` is the only caller of `api.uploadPage`** (searched `web/src` and `extension/`). `extension/` has no page upload; its "pages" references are meiko.studio's chapter `pages` field (`content.js:564-578`).
- **`LibraryPage.jsx:128-140` (`addImages`)** only calls `api.createImagesProject(name)`, then navigates to `/novel/${id}/pages`.
- **`api.js:49-74`** has all the page endpoints. `uploadPage(pid, file, { batch='', label='', name='' })` sends the raw file as the request body with a `?batch&label&name` query (58-65). `reorderPages(pid, ids)` must send every page id.

## 4. Consumers of `build`, `page_map` and batches

- **`web/src/components/ChapterReader.jsx`:**
  - Line 106: `sourceMode` defaults to `'photo'`.
  - Lines 427-429: `hasPhotos = (data?.page_ids || []).length > 0`.
  - Lines 740-758: Photo/Text toggle, styled `btn px-2 py-0.5 text-[11px]` with `btn-primary` for the active option and `btn-ghost` for the other.
  - Lines 760-769: `data.page_ids.map(id => <img src={api.pageImageUrl(pid,id)} className="w-full rounded-card border border-line" loading="lazy">)`, which shows the photos in stored `page_map` order.
- **Server side of that:** `page_ids` comes from `_chapter_page_ids` (`server/app.py:2188`, `2192-2196`), which returns `build.page_map[str(index)]`.
- **`ChapterReader.test.jsx`:** one smoke test with `page_ids` (116-120). Its fixture mirrors `chapter_detail` and includes `page_ids: []` (38-58).
- **Build endpoint (`server/app.py:1701-1792`):**
  - Merges `page_map` on append (1773-1778).
  - `used_page_ids` keeps already-built pages out of append builds (1717-1729).
  - `_refresh_built_chapters` (1666-1698) rebuilds chapters from the STORED id order and ignores `"*"`. So reordering or moving pages after a build does not change a built chapter's order or membership.
- **Other uses:** `PagesPage` BuildDialog only checks whether `build` exists (686). `ScansPage.jsx:72-73` reads `n.built` and `n.chapter_count` from `/api/scans` (`server/app.py:1317-1337`).
- **How joins behave in batch mode** (relevant to chapter boxes):
  - A `chapter` join inside a batch does NOT split it. `assemble_pages` just adds a blank line and restores the heading (`ocr_build.py:73-81`).
  - The first page of a run ignores `join_prev`, except that a `gap` produces a warning (`150-159`).
  - `stitch` computes seams between every pair of neighbouring pages, including across batch boundaries (`server/app.py:1617-1629`).
  - So the "new chapter" chip conflicts with explicit chapter boxes, and seams that cross a box boundary don't matter except for `gap`.

## 5. Reusable pieces

- **`web/src/reorder.js:13-25`, `reorderIds(ids, from, target)`:**
  - Returns null if the move does nothing or either id is missing.
  - Dropping onto a lower item lands after it; onto a higher item, before it.
  - Tests (`reorder.test.js`) cover down, up, front, end, always-a-permutation, missing ids, self-drop, and two items.
  - It is a single flat list. Moving between boxes would need a new helper (e.g. move to box X at position i), tested the same way.
- **`web/src/components/ui.jsx`:**
  - `PAGE_STATUS_STYLES` and `PAGE_STATUS_LABEL` (31-51).
  - `PageBadge({status})` (53-59), which pulses for `ocr-running`.
  - `Badge` for chapter status (61-67), `Dot` (69-76), `StatCard` (78-86), `Skeleton`, `SkeletonRows`, `SkeletonCards` (88-113), `ProgressBar({value,total})` (115-125).
  - `Modal({onClose, children, wide})` (127-155): Escape closes it, clicking the scrim closes it, and it focuses itself.
  - **Modal focus gotcha:** the effect depends on `[onClose]` and calls `ref.current?.focus()` each time it runs (129-134). An inline `onClose` (as at `PagesPage.jsx:433`) plus a parent that re-renders every 2.5s pulls focus back to the dialog on every poll tick. Pass a stable `useCallback`.
- **`web/src/confirm.jsx`:** `const confirm = useConfirm(); await confirm({ title, body, confirmLabel, cancelLabel, danger })` returns a boolean. The body uses `whitespace-pre-line`.
- **`web/src/toast.jsx`:** `const toast = useToast(); toast(message, kind='info'|'error')`. Toasts last 2.6s, sit bottom-right and use `row-in`.
- **`ErrorDialog`:** `showError(e, { context, onRetry, onAction })` (`components/ErrorDialog.jsx:9, 37`). It comes from the outlet context.
- **`web/src/components/Hint.jsx`:** `<Hint text="..." />` shows a "?" bubble and respects the global hints toggle in `web/src/hints.js` (`HintsContext`, `useHints`).
- **`web/src/guide.js`:**
  - `STEPS[2]` "Read a novel from photos" is at lines 40-54. Line 46 reads: `'Photos you add together count as one chapter. Add the next chapter's photos as a separate batch.'`
  - Line 45: "drop the images onto the 'Pages' tab". Line 51: "Build chapters".
  - `COST_NOTE` is at 159-164.
  - No test asserts on guide text.
- **`web/src/tasks.js`:** `isPageTask` and `PAGE_TASK_KINDS = ['ocr','ocr-verify']`. During OCR, `queue.current`/`pending` hold page SEQ numbers, not chapter numbers (40-53).
- **`web/src/prefs.js`:** private localStorage `read`/`write` helpers with `nr.*` keys. Use this pattern if box collapse state needs to persist.
- **`web/src/pages/ScansPage.jsx`:** an index of image novels. Rows link to `/novel/:id/pages` and use `pill-queued` / `pill-review` / `pill-translated` with counts. Uses `page page-narrow`, `card divide-y divide-line`, `rowhover`.

## 6. Styling conventions

- **`web/src/index.css` component classes:**
  - `.card` (surface, border, 16px radius) and `.sunken` (104-105).
  - `.btn` with `.btn-primary` (gold), `.btn-ghost` (bordered) and `.btn-quiet` (muted text) (107-119).
  - `.input` (121-127).
  - `.pill` with `-translated`, `-translating`, `-queued`, `-review`, `-english`, `-muted` (129-135).
  - `.rowhover` (185-186), `.subtab`/`.subtab-active` (201-208).
  - `.page` (max 72rem) and `.page-narrow` (212-213).
  - `.row-in` (259-260).
  - On coarse pointers, `.btn` gets `min-height: 44px` and `.tap` gives 44px targets (263-266).
  - `prefers-reduced-motion` handling (268-270).
- **Tailwind tokens (63-85):** `bg-page`, `bg-surface`, `bg-elevated`, `text-ink`, `text-muted`, `text-hint`, `border-line`, `border-line-strong`, `rounded-card` (16px), `rounded-btn` (10px), `font-reading`, `font-ui`, `font-korean`.
- **Raw CSS variables used inline:** `var(--b-muted-bg)` for image placeholders, `var(--border)` for hairlines, `var(--elevated)` for popovers, `var(--danger)` for destructive actions, `var(--accent)` for fills.
- **THEMING.md:** gold is never used for small body text. Edit tokens in both theme blocks.
- **Common sizes:** buttons use `px-3 py-1.5 text-sm` in headers and `px-2 py-1 text-xs` in panels; page titles use `font-reading text-2xl font-medium` with a `text-sm text-hint` subtitle.
- **Dashed boxes are empty states only.**
  - `border border-dashed border-line`: `PagesPage.jsx:374, 392`, `ImportPage.jsx:67, 115`, `ScansPage.jsx:50`, `SeriesDetailPage.jsx:360`, `PostingPage.jsx:65`, `PhonePage.jsx:49`.
  - `border-line-strong text-muted`: `ActivityPage.jsx:44`, `LibraryPage.jsx:379/387`, `ReviewPage.jsx:179`, `UpkeepPage.jsx:201/315`, `ArchivePage.jsx:70/88`, `ConsistencyPage.jsx:83`.
  - There is no dashed "+" tile anywhere.
- **Nearest "add" controls:** `GlossaryPage.jsx:428-430` (a `btn btn-ghost px-3 py-1.5 text-xs` "＋ Add term" toggle) and `SeriesDetailPage.jsx:277-303` (a select plus an "Add" button).
- **Drag-and-drop:** PagesPage is the only place in the app (rail rows 456-460, drop zones 388-392 and 405-410). ChaptersPage, GlossaryPage and SeriesDetailPage have none.
- **Patterns worth mirroring:**
  - Dropdown menu: `ProjectLayout.jsx:454-468`, an absolutely positioned `rounded-card border border-line ... shadow-lg` with `var(--elevated)`, item hover `hover:bg-[color-mix(in_oklab,var(--ink)_6%,transparent)]`, closed on `onMouseLeave`.
  - Segmented toggle: `SeriesDetailPage.jsx:330-341`, `btn-primary` for the active option and `btn-ghost` for the others.

## 7. File order, upload sequencing, and the poll

- **Order:** set by the FileList the browser gives (`PagesPage.jsx:165`), with no sort. Each page keeps its original filename as `page.name` (`server/pages.py:241`, via `safe_label`), so the UI could sort by name before upload, or show the name.
- **Sequencing:** one file at a time, and the first error stops the rest (`172-183`). The server writes pages in arrival order.
- **Concurrency:** nothing stops a second drop during an upload (see finding 4).
- **Poll gating:** the poll runs only while `running` is true. That flag is true for ANY job on the novel, including translation (`ProjectLayout.jsx:42, 112, 276`). Uploading never starts or pauses the poll.
- **Poll vs. local edits:** each tick replaces the rail with `setRail(...)`, which can briefly undo an optimistic reorder or edit until the next tick.
- **Poll vs. drag:**
  - `dropOn` reads the latest `pages` and skips the optimistic update if the page count changed (`305-312`).
  - The server rejects a list that isn't a full permutation with 400 (`server/pages.py:270-279`, `app.py:1443-1449`). The client then shows the error and reloads.
  - The ref-based `dragId` is not checked against a poll that removed the dragged page mid-drag; `reorderIds` returns null in that case.
- **Server limits:** 25 MB per image, 2000 pages per novel (`server/pages.py:38-39`). Accepted formats are JPEG, PNG and WebP; HEIC is refused with a message.

## 8. ProjectLayout outlet context and header

- **Context object** (`web/src/components/ProjectLayout.jsx:423-433`): `pid, status, data, loading, error, reload: load, loadPending, pendingCount, glossary, setRowStatus, setProjectMeta, showError, chapters, offline, counts, koreanTotal, done, remaining, running, submitting, log, live, totals, paused, waiting, queue, chapterQueue, totalQueued, enqueue, cancelQueue, stopAll, resumeJob, submitTask, resolveChapter, fixPronouns, fixPronounsFlagged`.
- **What PagesPage uses:** `pid, running, submitTask, showError, reload, setProjectMeta`.
- **Also available for chapter boxes:**
  - `chapters`: built chapter rows with `index` and `status`, which could be matched to boxes via `page_map`.
  - `queue`: `kind` plus page seq numbers during OCR.
  - `live.index`: the page currently being read.
  - `submitting`.
- **`reload(refresh)`** is `load(refresh=false, stillWanted=null)` (76-89). `setProjectMeta(updated)` merges into `data.project` (401-403). `submitTask(call, {context, onRetry, resetLog})` returns null on error, after showing it, and blocks a second call while one is in flight (263-308).
- **Header (441-447):**
  ```jsx
  {data?.total ?? '—'}{' '}{source_type==='images' ? 'chapters' : 'tabs'}
  {source_type==='text' ? ' · pasted text' : ''}{source_type==='images' ? ' · from photos' : ''}
  ```
  - `data.total = len(chapters)` from `GET /api/projects/{pid}/chapters` (`server/app.py:2086-2106`). It reads 0 until the first build, then `PagesPage` refreshes it with `reload(true)`.
- **Pages tab link:** shown only when `source_type === 'images'` (477-479). Route: `App.jsx:114`.
- **ChaptersPage** has no special empty state or link to Pages for an image novel with 0 chapters; it just shows "No chapters match this filter." (`ChaptersPage.jsx:365`).

## 9. Rules a new implementation must follow

- **Tests:**
  - Add every new api method (and fix `uploadPage`) to the `vi.mock('../api')` list.
  - Keep `react-router-dom` limited to `useOutletContext`, or extend the mock.
  - Use `fireEvent` and role/name queries.
  - Use fake timers in `try`/`finally`.
  - Make fixtures match the server shapes (batch `{id,label,at,count}`).
  - Start the file with a narrative "why" comment.
  - Put pure ordering logic in a module like `reorder.js` with its own `*.test.js`.
- **Page work** (read and verify) must go through `submitTask`, never call `api.readPages` directly.
- **Poll errors** must stay quiet (shown inline), never as a dialog.
- **Autosave** must be flushed, not cancelled, when leaving a page.
- **`has_text` and `chars`** must be recomputed in any optimistic row merge.
- **Modals:** use `confirm`/`toast`/`Modal` from the shared modules, and pass a stable `onClose` to `Modal`.
- **Styling:** use the token classes (`card`, `sunken`, `rowhover`, `btn-*`, `pill-*`, `border-line`/`border-line-strong`, `rounded-card`, `text-hint`) and avoid raw colours.
- **Guide text:** `guide.js:46` (and 45 and 51) needs rewording to match explicit chapter boxes.

## Backend map

# Backend research: Pages tab, batches, build, OCR, stitch

**Paths:** `ROOT = C:/Users/Mommy/Documents/night-reader/night-reader`. Short names used below:
- `pages.py` = `ROOT/server/pages.py`
- `ocr_build.py` = `ROOT/server/ocr_build.py`
- `app.py` = `ROOT/server/app.py`
- `projects.py` = `ROOT/server/projects.py`
- `ocr.py` = `ROOT/translation_bot/ocr.py`
- `ocr_join.py` = `ROOT/translation_bot/ocr_join.py`
- `prompts.py` = `ROOT/translation_bot/prompts.py`
- `locks.py` = `ROOT/translation_bot/locks.py`
- `test_pages_api.py`, `test_pages_store.py`, `test_ocr_build.py`, `test_bundle_images.py` are in `ROOT/tests/`
- `PagesPage.jsx` = `ROOT/web/src/pages/PagesPage.jsx`
- `api.js` = `ROOT/web/src/api.js`
- `ChapterReader.jsx` = `ROOT/web/src/components/ChapterReader.jsx`
- `repair_library.py` = `ROOT/tools/repair_library.py`

## 1. The manifest (`pages.py`) and the `/pages` endpoints

**`pages.py`**
- **Ordering note (13-16):** a filename carries `seq`, never position. Reorder rewrites only the manifest.
- **Limits:** `MAX_IMAGE_BYTES` is 25 MB (38). `MAX_PAGES_PER_PROJECT` is 2000 (39).
- **Statuses:** listed at 41-51. `APPROVED_STATUSES = (ok, edited)` (54). `JOIN_KINDS = sentence/paragraph/chapter/gap` (56). `_PAGE_ID_RE` is 8 hex characters (58).
- **`new_doc` (138-140):** `{"version":1,"next_seq":1,"pages":[],"batches":[],"build":None,"totals":{"cost_usd":0.0}}`
- **`load_pages` (143-169):**
  - A missing file reads as `new_doc()`. A corrupt file is quarantined, then reads as `new_doc()`.
  - It then calls `setdefault` for version, pages, batches, build and totals, and recomputes `next_seq` from the highest `seq` when it is absent.
  - Nothing ever reads `version`. I grepped for `.get("version")` and `["version"]`; the only hit is `series.py:470`, which is a different file.
- **`mutate_pages` (177-187):** `with file_lock(pages_file(pid)): doc = load_pages(pid); yield doc; save_pages(pid, doc)`. There is no try/finally, so an exception inside the block (including `HTTPException`) means nothing is saved.
- **`safe_label` (199-202):** applies `_UNSAFE_LABEL_RE = [^\w .\-()\[\]]` (59) and truncates to 120 characters. It strips `: , ! ? ' "` and similar, which matters if a batch label becomes the chapter title.
- **`find_by_hash` (209-213):** a global sha256 lookup across the whole novel.
- **`new_batch` (216-220):** appends `{"id": uuid8, "label": safe_label(label), "at": now, "count": 0}`.
- **`_touch_batch` (223-228):** recounts one batch's pages.
- **`add_page` (231-267):**
  - Sets `seq = next_seq` and increments it.
  - Sets `file = f"page-{seq:04d}.{ext}"` and `"batch": batch` (244), with `join_prev ""`, `join_prev_source ""` and `join_glue "space"`.
  - Always appends to the end of the list: `doc.setdefault("pages", []).append(page)` (265).
- **`reorder` (270-279):** must receive a full permutation of every page id in the novel (275), otherwise it returns False. It never touches `batch`, `join_prev` or `build`.
- **`delete_pages` (282-290):** removes the records and recounts every batch. Batches are never deleted, so empty batches with `count: 0` linger.
- **`summary` (303-318):** returns the pages without `text`/`raw_text`, `batches` verbatim, `build` (including `page_map` and `used_page_ids`), counts and totals.

**Endpoints in `app.py`**
- **Route-order note (1351-1354):** literal `/pages/...` routes must be declared before `/pages/{page_id}`.
  - Literal routes: POST `/pages` (1371), GET `/pages` (1431), `/reorder` (1443), `/delete` (1456), `/ocr` (1589), `/verify` (1595), `/stitch` (1606), `/build` (1701).
  - Parameterised routes after them: GET `/{page_id}` (1795), GET `/{page_id}/image` (1802), POST `/{page_id}` (1825).
  - A new literal GET must go before 1795 and a new literal POST before 1825. The test at `test_pages_api.py:202-207` only checks reorder and delete.
- **`upload_page` (1371-1428):** an `async def`. It takes the image as a raw request body with query parameters `batch`, `label` and `name`.
  - Size is checked while streaming (1386-1392), then the bytes are sniffed (1399-1401).
  - Inside `mutate_pages` the order is: duplicate check (1405-1409), then the 2000-page limit (1410-1412), then batch assignment (1414-1415), then `add_page`, then the file write. If the write fails, the record is removed and a 500 is raised (1419-1426).
  - Response: `{"page","duplicate":False,"batch":batch_id,"total"}` (1427).
- **`list_pages` (1431-1436):** `summary(load_pages)`.
- **`reorder_pages` (1443-1449):** a sync endpoint. It returns 400 if the ids are not a permutation.
- **`delete_pages` (1456-1467):** removes the records, then unlinks the files outside the lock. It does not touch `build`.
- **OCR / verify:**
  - `_queue_page_work` (1537-1586) is the shared implementation.
  - `run_page_ocr` (1589) and `run_page_verify` (1595) are both `async`.
  - `_resting_status` (1481-1501) and `_release_queued_pages(pid, seqs)` (1504-1534) cover the abort and release paths.
- **`stitch_pages` (1606-1655):** `async`. Covered in section 4.
- **Build:** `BuildChapters` (1658-1663) defaults to `mode="batch"`, `include="approved"`, `append=True`, `force=False`. `_refresh_built_chapters` is at 1666-1698 and `build_pages_into_chapters` at 1701-1792.
- **`update_page` (1825-1861):** `PageUpdate` (1816-1822) accepts text, status, join_prev, join_glue, heading and hint. There is no `batch` field, and no endpoint anywhere changes a page's batch or creates, renames, reorders or deletes a batch.
  - Editing text sets the status to `edited` (1846-1847).
  - Setting `join_prev` marks the seam as the user's: `join_prev_source="user"` (1852).

## 2. Build: batch mode, append, `used_page_ids`, `page_map`

**`ocr_build.py`**
- **`usable_pages` (25-40):**
  - Uses manifest order.
  - Drops skipped pages and pages with empty text.
  - With `include="approved"` it also drops any page not in `ok`/`edited` (37-38). This is silent; nothing warns.
- **`assemble_pages` (43-86):**
  - The first page in the list starts the text, so its `join_prev` is ignored (58-60).
  - An empty `join_prev` is treated as `"paragraph"` (62).
  - A `gap` produces a warning and is then joined as a paragraph (65-71).
  - A `chapter` join inserts `\n\n` plus the page text, restoring the heading if it is missing (73-81). In batch mode this stays inside one chapter.
  - Everything else goes through `join_text`.
- **`_batch_runs` (89-103):** groups *contiguous* runs of the same batch id (`if not runs or batch != current_batch: runs.append([])`). The order of the `batches` list is never used; chapter order comes purely from the order of `doc.pages`.
- **`_batch_title` (106-113):** the batch label, else the first page's heading, else `f"Chapter {index}"`.
- **`build_chapters` batch branch (142-165):**
  - Each run becomes one chapter via `make_chapter`.
  - A `gap` on the first page of a run after the first run produces a boundary warning (154-159).
  - It records `page_map[str(index)] = [p.get("id") for p in run]` (164). That list holds only the usable pages, in the order they were in at build time.
- **Other modes (167-177):** heading, separator and single assemble everything, split it, re-index it, and record `page_map = {"*": [...all usable ids]}`.

**`build_pages_into_chapters` (`app.py` 1701-1792)**
- It reads the doc and the existing `source.json` without a lock (1711-1713).
- **Append (1716-1730):**
  - `built_ids = set(prev_build["used_page_ids"])`.
  - `refreshed = _refresh_built_chapters(existing, doc, prev_build["page_map"], include=body.include)`.
  - The doc is then filtered so already-built pages are excluded: `doc = {**doc, "pages": [p for p in doc["pages"] if p.get("id") not in built_ids]}` (1727-1729).
  - `start_index = len(existing) + 1`.
- **Non-append (1731-1739):** returns 400 if any chapter is in `DONE_STATUSES` and `force` is not set. Otherwise it starts again from index 1.
- If there are no new chapters and nothing was refreshed, it returns 400 "No pages are ready to build" (1748-1751).
- **Combine and write:** `combined = existing[:start_index-1] + chapters`, renumbered from 1 (1753-1755). Then `cache_source` writes `source.json`, the chapter cache is dropped, and padding is normalised (1757-1763). `source.json` stores only `{title, paragraphs}`, and the index is the position in the list (`text_source.py:147-158`).
- **Bookkeeping (1765-1778):**
  - `used` is the ids of the usable pages in the *filtered* doc.
  - The new record is `{"mode","at","chapters","warnings","page_map": {**prev_page_map, **page_map}, "used_page_ids": sorted(prior | set(used))}`.
  - On a non-append build `prev = {}`, so the map is replaced rather than merged.
- **Refresh warning (1780-1788):** "Chapter N updated from pages you corrected … re-translate".

**`_refresh_built_chapters` (1666-1698)**
- It skips the `"*"` key.
- It rebuilds each chapter from the stored ids, in their stored order:
  ```python
  run = [by_id[i] for i in ids if i in by_id and i in usable]   # 1686
  if not run: continue                                          # 1687-1691
  rebuilt = ocr_build.make_chapter(int(key), target.title, text)  # 1693
  ```
- It keeps the old title and only replaces the paragraphs if they differ.
- `Chapter.metrics.content_hash` is a property (`docs_extract.py:71-78`), so a change of text flips `source_changed` (`app.py:656`).

**What happens today in each case**

- **(a) Reordering pages inside a built chapter**
  - `reorder` only permutes `doc.pages`. `page_map`, `used_page_ids` and `join_prev` stay as they were.
  - Refresh iterates the stored ids, so the new order is ignored. If nothing else changed, Build returns 400.
  - The reader's photo strip (`page_ids`, from `_chapter_page_ids` at `app.py:2192-2196` and shown at `ChapterReader.jsx:427-429` and `762`) keeps the old order.
  - If the user presses Stitch after reordering, `join_prev` is recomputed for the new neighbours but refresh applies it in the old order. The joins no longer match the page order, and the chapter is reported as "updated from pages you corrected".
  - Only a full rebuild (`append=false`, plus `force` if anything is translated) applies the new order, and that renumbers the whole novel.
- **(b) Moving a page into another batch**
  - There is no API for this. `batch` is set once in `add_page` (`pages.py:244`).
  - Dragging only changes position. Because runs are contiguous, a page from batch b1 dropped inside b2's run splits it into b2 | b1 | b2, which builds as three chapters (`test_ocr_build.py:169-174`).
  - If the pages are already built there is no effect at all: they are filtered out by `used_page_ids` and refresh uses the stored ids.
- **(c) Adding photos to a batch that is already built** (upload with `?batch=<known id>`)
  - The page is appended to the end of `doc.pages`, after any later batches.
  - It is not in `used_page_ids`, so the next append build makes it its own new chapter at index `len(existing)+1`, with the same label/heading title or "Chapter N".
  - Refresh never sees it.
  - The UI cannot reach this today, because each drop starts with `batch=''` (`PagesPage.jsx:168`). It is exactly the path a "+" tile on an existing chapter box would take.
- **(d) Deleting a page from a built chapter**
  - The record and file are removed, but `page_map` and `used_page_ids` still hold the id. `source.json` is unchanged until the next Build.
  - On that Build, refresh drops the id (the `i in by_id` check at 1686) and rebuilds the chapter without it, with the re-translate warning.
  - The following page's `join_prev` still describes the deleted page until Stitch is run again.
  - If every page of a chapter is deleted or un-approved, the chapter is left untouched (1687-1691). It can only be removed by a full rebuild.
  - The reader's `page_ids` still lists the deleted id, so that image returns 404 (1810-1812).
- **(e) A page that is not yet approved when Build is pressed** (new, queued, needs-check or failed, with `include="approved"`)
  - It is silently left out of the chapter, `page_map` and `used_page_ids`.
  - The page after it is joined to the page before it, using a `join_prev` that was computed against the excluded page.
  - Once it is approved, the next append build turns it into its own chapter at the end. Story order breaks with no warning.
  - The UI does not guard against this; `PagesPage.jsx:638-677` only shows the approved count.
- **(f) Stale joins**
  - `join_prev` lives on the following page and is never reset by reorder, delete or insert.
  - Seams the user set by hand (`join_prev_source == "user"`) are never revisited, even after their neighbour changes.
  - New pages have `join_prev ""`, which builds as a paragraph break. Build never stitches automatically, so if Stitch is skipped, every page break becomes a paragraph break.

## 3. The OCR worker path

- **Queuing:**
  - Queue items are page seqs: `items = [(int(p["seq"]), True, kind) for p in wanted]` (1557).
  - `wanted` is in manifest order and filters out skipped pages (1542-1553).
  - Only the pages the queue actually accepted are marked `queued`, after their status is stashed as `prior_status` (1564-1576). If none were accepted the endpoint returns 409 (1577-1583).
- **Dedup key:** `_queue_key` returns `f"{'pg' if kind in PAGE_TASK_KINDS else 'ch'}:{idx}"` (4496-4505). The constants are defined at 4479-4484, and `Job.enqueue` (4589-4600) skips anything already queued.
- **Dispatch:** the worker routes page kinds to `_run_page_item` (5031-5038).
- **`_run_page_item` (4909-5006):**
  - Finds the page by `seq`: `page = next((p for p in doc["pages"] if p.get("seq") == idx), None)` (4921). If the page is gone the item is skipped.
  - Sets the status to `ocr-running` and publishes events carrying `index=seq` and `page_id`.
  - Runs `_run_page_task` in the threadpool and applies the result by page id through `_apply_page_result` (4880-4898).
  - Abort and refusal restore `prior_status`. A rate limit puts the item back at the head of the queue. Any other exception marks the page failed.
  - Cancelling releases dropped pages by seq (5349-5351).
- **`_run_page_task` (4823-4877):**
  - Verify (4833-4849) sends the image plus that page's own text.
  - OCR (4851-4877) sends the image plus the page's `hint`. The result overwrites text, heading, the three seam booleans, status, `ocr`, `verify=None` and `hint=""`. It never touches `join_prev`, `batch` or position, but it does overwrite a hand edit made while the OCR was running.
- **No neighbour context anywhere:**
  - `transcribe_page` (`ocr.py:257-273`) gives the model one image path and a system prompt from `build_ocr_prompt(hint)` (`prompts.py:292-304`). The prompt says "given the absolute path of ONE page image" (`prompts.py:177`).
  - `verify_page` (`ocr.py:276-293`) uses only the image and that page's text.
  - Order matters only for the queue's FIFO order, and for stitch and build.
  - `seq` is monotonic and never reused (`test_pages_store.py:102-111`).

## 4. Stitching

- **What it compares:** `pages = [p for p in doc["pages"] if status != skipped]` (1617-1618), then `for prev, cur in zip(pages, pages[1:])` (1622).
  - This is every non-skipped page across all batches, built or not, including unread or empty pages.
  - Seams the user set are skipped (1623-1624).
  - The decision is stored on the later page, `decided[cur["id"]]` (1626).
- **Model calls:**
  - Unsure seams (confidence below `LOW_CONFIDENCE` 0.6, `ocr_join.py:26` and `57-58`) are capped at the first `max_model_seams` in manifest order (default 25; 1603, 1633).
  - Empty-text seams get confidence 0.3 (`ocr_join.py:130-131`), so they are sent to the model with empty text.
  - Decisions marked `"model"` are not protected, so every re-run recomputes and re-asks the same first 25, and seams after the 25th never reach the model.
  - Only about 200 characters each side of a seam are sent (`ocr.py:45`, `296-307`). The stitch prompt is at `prompts.py:265-289`.
- **Race:** the snapshot is taken at 1616, the model call is awaited at 1636, and the result is written by page id at 1644. A reorder during the model call writes joins computed for the old neighbours.
- **`propose_join` (`ocr_join.py:116-174`):** a heading on the next page returns `chapter` (135-136). It never returns `gap`. `join_text` (177-191): `sentence` joins with `""` or `" "`; everything else joins with `\n\n`.
- **Between two batches:** the seam is still computed and stored on the next batch's first page. In batch-mode builds it is ignored, except that a `gap` produces a "missing page where this chapter starts" warning (`ocr_build.py:154-159`). In heading, separator and single modes it is applied, so a `sentence` join would glue two user chapters together.
- **Built chapters:** re-stitching after a build can change the joins of built pages. The next append then "refreshes" those chapters even though the user corrected nothing.

## 5. Other consumers, bundle export/import, schema version

- **`batch`:** used only in `pages.py` (`new_batch`, `_touch_batch`, `add_page`, `summary`), `ocr_build.py` (`_batch_runs`, `_batch_title`) and `upload_page`. The "batch" hits in `docsmake.py:148-175` and `docexport.py:149` are Google Docs API request batches and unrelated. `extension/` has no hits.
- **`page_map`:** produced by `build_chapters`, consumed by `_refresh_built_chapters`, merged by the build endpoint, and read by `_chapter_page_ids` (`app.py:2192-2196`). That feeds the chapter payload's `"page_ids"` (2184-2188), which drives the reader's Photo/Text toggle (`ChapterReader.jsx:427-429`, `762`).
- **`used_page_ids`:** used only by the build endpoint (1718, 1774-1778).
- **`scans_overview` (1317-1337):** uses counts, `bool(build)` and `totals.cost_usd`.
- **`repair_library.py:250-297`:** round-trips the JSON and only resets queued/running pages to `new`, so unknown fields survive.
- **`source.json`** is written for image novels only by the build endpoint (1757). The `paragraph/source` endpoint (2642) is read-only.
- **Bundles:** `pages.json` is in `_BUNDLE_FILES` (`projects.py:229-232`). Export copies the raw file (275-278) and import writes the raw bytes back (349-354). Images travel only with `include_images` (242, 267). Any new fields (batch title, order, and so on) round-trip with no code changes. `test_bundle_images.py:177-201` checks the round trip.
- **Version:** only set via `setdefault` (`pages.py:139`, `160`) and never read or branched on. A new top-level key needs both a `setdefault` in `load_pages` and an entry in `new_doc`.
- **Existing data on this machine:** the only image novel is `ROOT/projects/13109ae81d44` (source_type `images`, created 2026-10-01 17:58). Its `pages/` folder is empty and it has no `pages.json`, so there is no real manifest data to migrate.

## 6. Backend test conventions

- There is no `conftest.py`. Each test file adds `ROOT` to `sys.path` and can run on its own via `pytest.main([__file__, "-v"])`. No model is ever called.
- **API tests (`test_pages_api.py`):**
  - The `client` fixture (43-54) patches both `pj.PROJECTS_DIR` and `P.PROJECTS_DIR` to `tmp_path/"projects"`. Both are needed because `pages.py:31` copies `PROJECTS_DIR` by value.
  - It also sets `A.load_global_config` to `lambda: Config()`, clears `A._chapter_cache`, and builds `TestClient(A.app, base_url="http://localhost", client=("127.0.0.1", 50000))`.
- **Fake images:** magic bytes plus padding, e.g. `JPEG = b"\xff\xd8\xff\xe0" + b"\x00"*256`. `JPEG2` uses `\x01` padding so its sha256 differs; `PNG` and `HEIC` follow the same pattern (37-40).
- **Helpers:**
  - `_novel` posts to `/api/projects/images` (57-60).
  - `_upload(client, pid, data, **params)` sends the raw body with `params=` (63-65).
  - `_set` posts to `/pages/{id}` (68-71).
  - `_two_page_chapter` threads the `batch` id (243-256).
- To avoid spawning a worker, tests monkeypatch `A._enqueue_task` (547, 563, 583-585).
- **Store tests (`test_pages_store.py`):** the `project` fixture patches only `P.PROJECTS_DIR` (42-47). `_add(doc, ...)` calls `P.add_page` directly (50-51), and the functions are tested on plain dicts.
- **Build tests (`test_ocr_build.py`):** pure dict helpers `page(text, join=, glue=, status=, batch=, heading=, name=)` with a global seq, and `doc(pages, batches)` (32-45).
- **Representative test (`test_pages_api.py:259-270`):**
```python
def test_one_drop_is_one_chapter_but_separate_drops_are_not(client):
    pid = _novel(client)
    first = _upload(client, pid, data=JPEG).json()
    _upload(client, pid, data=JPEG2, batch=first["batch"])
    _upload(client, pid, data=PNG)  # a separate drop
    for page in client.get(f"/api/projects/{pid}/pages").json()["pages"]:
        _set(client, pid, page["id"], text="본문입니다.", status=P.STATUS_OK)
    built = client.post(f"/api/projects/{pid}/pages/build", json={"mode": "batch"})
    assert built.json()["chapters"] == 2, "two drops make two chapters, not three"
```
- Build and refresh behaviour is also covered at 306-468.

## 7. Concurrency, uploads, limits, duplicates

- **Lock:** `file_lock` is a process-wide `threading.RLock` per resolved path (`locks.py:29-41`). It only works within one process; `repair_library.py:286-290` warns when the app is open.
- **Where the locked blocks run:**
  - `upload_page`, `stitch_pages` and `_queue_page_work` (called from the async `/ocr` and `/verify` routes) run `mutate_pages` on the event-loop thread.
  - `upload_page` also writes up to 25 MB inside that lock (1404-1428).
  - The worker's `_set_page_status` and `_apply_page_result` also run on the loop.
  - No `with` block on the loop contains an `await`, so the re-entrant lock cannot interleave. The sync endpoints (reorder, delete, update, build) run in the threadpool and contend normally.
- **Build is not serialised:** its reads at 1711-1713 are unlocked, and only the final record write is locked (1766).
- **Batch assignment:** `batch_id = batch if (batch and batch in known) else pages_mod.new_batch(doc, label)` (1414-1415). An unknown or empty id silently creates a new batch with a new server id, so the client must read `res.batch`. `label` is only used when a batch is created, and the UI always sends `label: ''` (`PagesPage.jsx:174`). There is no way to create an empty batch (a chapter box) without uploading a photo.
- **Upload order:**
  - The frontend uploads sequentially in `Array.from(files)` order with no sorting, and stops the rest of the drop on the first error (`PagesPage.jsx:164-190`).
  - So `seq` and manifest order follow the browser's file-list order.
  - The server always appends the new page to the end of the list, whichever batch it belongs to.
- **Duplicates:**
  - The check runs before the page limit and before a batch is created: `existing = find_by_hash(doc, digest)` → `return {"page": existing, "duplicate": True, "total": ...}` (1405-1409).
  - The response has no `batch` key, and the existing page stays in its original batch and position.
  - So the same photo uploaded into a different chapter is a silent no-op (the UI just counts it as "already here"). The existing record's `batch` field is what tells you which chapter it is in.
  - Re-uploading a photo after deleting it creates a new page with a new seq and id. On an append build, that page becomes a new chapter.
- **Limits:** 2000 pages per novel (`pages.py:39`, checked at `app.py:1410-1412`) and 25 MB per image (`pages.py:38`, checked at `app.py:1386-1392`).

## 8. Gaps the chapter-box redesign has to close

1. **Missing endpoints:** create an empty batch with a title, rename it, reorder batches, and move or reorder a page within or across batches. Today `reorder` needs every page id in the novel.
2. **One source of truth for order.** Today chapter order is the contiguous runs in `doc.pages`, and the `batches` list order is ignored. Either upload inserts after the last page of its batch, or build groups by batch id in batch order.
3. **Built chapters must follow later changes.** Refresh would need to derive each chapter from the batch's current members and order rather than the stored `page_map` ids, then update `page_map` and `used_page_ids` (and probably the title). Otherwise cases (a), (c), (d) and (e) stay broken.
4. **Joins:** reset `join_prev` for every seam a move touches, scope stitching to seams within a batch, and warn about or block building a batch that has unapproved pages.
5. **Duplicates:** the per-novel duplicate rule has to be surfaced in the UI, e.g. "this photo is already in Chapter X".
6. **Labels as titles:** `safe_label` strips punctuation from labels, which affects chapter titles if a batch label becomes the title.
7. **Manifest compatibility:** new manifest fields need defaults in `load_pages`. The UI help text at `ROOT/web/src/guide.js:46` describes the current one-chapter-per-drop model and will need updating.

## Ordering-risk audit

I confirmed 16 ways the page order or the chapter grouping can get mixed up today. The worst ones are silent. Two of them, #1 and #2, can happen with nothing unusual going on.

**How the risks were checked:** I read the code end to end. I also ran the build and splitting functions directly in Python with `-B`, so nothing was written to disk. I did not drive the HTTP API, because that would have created project folders.

**Files** (each finding below names the file and line):
- C:/Users/Mommy/Documents/night-reader/night-reader/web/src/pages/PagesPage.jsx
- C:/Users/Mommy/Documents/night-reader/night-reader/web/src/components/ChapterReader.jsx
- C:/Users/Mommy/Documents/night-reader/night-reader/server/app.py
- C:/Users/Mommy/Documents/night-reader/night-reader/server/pages.py
- C:/Users/Mommy/Documents/night-reader/night-reader/server/ocr_build.py
- C:/Users/Mommy/Documents/night-reader/night-reader/server/docexport.py
- C:/Users/Mommy/Documents/night-reader/night-reader/translation_bot/text_source.py
- C:/Users/Mommy/Documents/night-reader/night-reader/translation_bot/ocr_join.py
- C:/Users/Mommy/Documents/night-reader/night-reader/translation_bot/ocr.py
- C:/Users/Mommy/Documents/night-reader/night-reader/translation_bot/chapter_numbers.py

## Confirmed risks, most severe first

**1. CRITICAL – A page can silently drop out of the middle of a chapter, then come back later as its own chapter at the end.**
- **Where:**
  - `ocr_build.py:35-38`: pages with no text are always left out, and with the default setting, pages still marked "needs-check" are left out too.
  - `PagesPage.jsx:638`: the Build dialog resets "Which pages to use" to "Only pages I've accepted or edited" every time it opens.
  - `app.py:1686` (the refresh only uses the chapter's stored page list) and `app.py:1727-1730` (pages already built are filtered out, and new chapters start after the last one).
- **Scenario:** a chapter is made of photos p1, p2 and p3. Claude was only medium-confident about p2, so it is still "needs-check". You press Build with the defaults. Later you accept p2 and press Build again (adding is the default).
- **Behaviour (simulated):**
  - Build 1 makes the chapter from p1 and p3 only, as `"그는 문을 돌아오지 않았다."`. p3's "sentence continues" join was worked out against p2, so p1's half-sentence is glued straight onto p3.
  - It gives no warning.
  - Build 2 makes a new chapter 2, titled "Chapter 2", containing only `"열고 나갔다. 그리고"`. Chapter 1 never gets p2 back.
  - The same happens to a page whose reading failed and is still empty.
- **Likelihood:** high. Claude marks any page it wasn't highly confident about as "needs-check" (`app.py:4871`).

**2. CRITICAL – If one photo fails partway through an upload, the retry splits the chapter in two.**
- **Where:**
  - `PagesPage.jsx:178-181`: the upload loop stops at the first error and skips the rest of the files.
  - `app.py:1405-1409`: when a photo is already uploaded, the server's answer doesn't say which batch it is in.
  - `app.py:1414-1415`: an upload with no batch id starts a new batch.
- **Scenario:**
  - You drop 10 photos and the 4th is an iPhone HEIC photo, or is over 25 MB. Photos 1-3 go into batch 1 and photos 5-10 are never sent.
  - You convert the photo and drop all 10 again. Photos 1-3 are recognised as "already here", and the photos that are new this time go into a new batch 2.
- **Behaviour:** building by batch makes two chapters out of one, and the second may be titled with the wrong chapter number. The same happens after any network error.

**3. CRITICAL – Two drops at once get interleaved.**
- **Where:** `PagesPage.jsx:43,164-190,339,405-410`. The "uploading" flag is only used to show progress. Nothing stops a second upload while the first is running, and the "Add photos" button and the drop area stay active.
- **Scenario:** you drop chapter 5's photos, then drop chapter 6's photos while chapter 5's are still uploading.
- **Behaviour:** the server adds each photo to the end of the list as it arrives, giving A1, B1, A2, B2… (simulated). The build then makes **4 one-page chapters**, and the order of pages in the list is mixed.

**4. HIGH – Photos are uploaded in whatever order the browser hands them over.**
- **Where:** `PagesPage.jsx:165` does `Array.from(files).filter(f => f.size)`. Nothing in `web/src` sorts photos by name (in number order or otherwise) or by date before upload. `PhonePage.jsx` has no upload code at all.
- **Scenario:** Windows file dialogs and Explorer drags are commonly reported to put the focused or grabbed file first. I can't verify that from the code. Note that plain alphabetical sorting would also be wrong (`IMG_10` before `IMG_9`).
- **Behaviour:**
  - The wrong order is permanent, both in the page list and in the built chapter.
  - If the page with the chapter heading isn't first, the chapter title becomes "Chapter N" (`ocr_build.py:106-113`) and the heading line ends up in the middle of the text.

**5. HIGH – Once a chapter is built, its pages and their order are frozen.**
- **Where:**
  - `app.py:1686`: the refresh rebuilds a chapter from its *stored* page list, in stored order.
  - `app.py:1727-1730,1748-1751`: refresh and append behaviour, and the error message.
- **Scenario:** you fix the page order in the list after building, then press Build.
- **Behaviour:**
  - The text comes out the same, so you get a 400 error: "No pages are ready to build. Read the pages first…", which is misleading.
  - A photo dragged into an already-built chapter, or uploaded to it, becomes a **new chapter at the end** instead.
  - The only fix is a full rebuild (`1731-1739`). It renumbers chapters and has to be forced if any are translated. The document export (`docexport.py:41-58`) matches English files to chapters by number and never checks whether the Korean changed, so after a regrouping rebuild, English can come out under the wrong chapter.

**6. HIGH – Building with the default page setting can strip pages out of chapters that were already built.**
- **Where:** `app.py:1676,1686`; `PagesPage.jsx:638`.
- **Scenario:** you build the first time with "Every page that has text". Later you build again and leave the default "Only pages I've accepted or edited".
- **Behaviour:** every earlier chapter is quietly rebuilt *without* its needs-check pages, leaving holes. The only notice is a warning that the app never displays (#7).

**7. HIGH – Build warnings are never shown.**
- **Where:**
  - `PagesPage.jsx:434-440` only reads how many chapters were added and the updated novel details.
  - The server stores the warnings (`app.py:1776`) and returns them (`1790`), but no screen displays them. I searched `web/src`.
- **Behaviour:** "A page may be missing" warnings and "Chapters N were updated" notices are invisible. Those warnings are the only safety net for missing pages.

**8. HIGH – Moving a photo between chapters by dragging breaks chapters apart.**
- **Where:**
  - `ocr_build.py:89-103`: a chapter is a *run of neighbouring pages* from the same batch.
  - `app.py:1816-1822`: there is no way to change which batch a page belongs to.
  - `pages.py:265`: an upload into an existing batch is added at the end of the *whole* list.
  - The page list doesn't show where batches start and end.
- **Scenario:** batches are a1, a2, a3 and b1, b2, b3. You drag a3 below b1.
- **Behaviour:** the build makes 4 chapters: a1+a2, b1, a3, b2+b3. A test (`test_reordered_pages_group_by_arrangement_not_upload_order`) pins this as intended. There is no way to move a photo from one chapter to another.

**9. MEDIUM-HIGH – The "new chapter" join chip does nothing when building by batch (the default).**
- **Where:** `ocr_build.py:148` builds each batch without its separator, and `73-81` only adds a paragraph break for a "chapter" join. The page editor has no box for the chapter heading.
- **Behaviour (simulated):** one drop holding pages with 제4화 and 제5화 builds as one chapter, with "제5화" as a paragraph in the middle. The chips in the list are the only chapter markers you can see, but in batch mode they don't decide where chapters split.

**10. MEDIUM – Joins between pages go stale.**
- **Where:**
  - `pages.py:270-279`: reordering never resets a page's join.
  - `app.py:1623`: joins you set by hand are never checked again.
  - `app.py:1616→1636→1644`: "Work out the joins" reads the page order, waits for Claude, then writes the results back by page id. The list stays draggable and the chips stay clickable during that wait, and a chip you click then gets overwritten.
- **Behaviour:** after you reorder, delete, skip or leave a page out, the page that now follows still keeps the join it had with its old neighbour. In the worst case a "sentence continues, no space" join glues words from unrelated pages together mid-word.

**11. MEDIUM – Leftover drag state in the page list.**
- **Where:** `PagesPage.jsx:290-295,456-460`. The dragged page is only forgotten when it's dropped on another row, and nothing clears it when a drag ends.
- **Scenario:** you start dragging a row and release it elsewhere, or press Esc. Later you drop photo files onto a row.
- **Behaviour:** the page from the cancelled drag is silently moved, and the dropped files are not uploaded. Even with no cancelled drag, files dropped onto a row are silently ignored.

**12. MEDIUM – Adding chapters always puts them at the end.**
- **Where:** `app.py:1730,1753`.
- **Behaviour:** a missed chapter dragged to its correct place in the list still becomes the last chapter. The order in the page list and the chapter order drift apart without any warning.

**13. MEDIUM – Missing-page detection never reaches later pages.**
- **Where:**
  - `app.py:1633` asks Claude about at most 25 unclear page joins.
  - Running it again asks about the same first 25, because only joins you set by hand are kept (`1623`).
  - Clear-cut joins are never sent to Claude (`ocr_join.py:135-158`), and the simple rules alone never report a gap.
- **Behaviour:** joins after the first 25 unclear ones never get the "page missing?" check. Pages in the wrong order are never flagged if each page happens to end on a full sentence.

**14. MEDIUM – Wrong splits in the "heading" and "separator" build modes.**
- **Where:**
  - `text_source.py:91`: the heading pattern is matched on every line, with no length limit.
  - `text_source.py:50`: in separator mode, any first line of 40 characters or fewer becomes the chapter title and is removed from the text.
  - `text_source.py:35`: a `***` line counts as a separator.
- **Behaviour (simulated):**
  - In heading mode, `"서장은 고개를 끄덕였다."` ("the police chief nodded") became chapter 2's **title** and left the text.
  - `"3장의 사진을…"` and `"2부 리그로…"` also match the heading pattern.
  - The join rules call that line a new chapter with 0.95 confidence, so it is never sent to Claude.
  - In separator mode, `"「가자.」"` became the chapter title.

**15. LOW-MEDIUM – The reader's Photo view.**
- **Where:** `ChapterReader.jsx:760-769`; `app.py:2192-2196`.
- **Behaviour:**
  - Photos appear in the order they had when the chapter was built.
  - The list of photos isn't updated when a chapter is refreshed. A photo whose page was left out still shows, and a deleted page shows a broken image.
  - Chapters built in heading, separator or single mode show no photos.

**16. LOW-MEDIUM – Re-uploading the same photo.**
- **Where:** `app.py:1405-1409`.
- **Behaviour:**
  - If the same photo was wrongly included in an earlier drop, it stays in that chapter. The toast says "1 already here" but not where.
  - A page you deleted and then drop again comes back at the end, in a new batch.

**17. LOW – A list refresh during reading can undo a drag.**
- **Where:** `PagesPage.jsx:76-85,305-314`.
- **Scenario:** while pages are being read, the list refreshes every 2.5 seconds.
- **Behaviour:** a refresh that started before your drag reached the server can put the old order back on screen. A second drag made from that old order then silently undoes the first.

**Knock-on effect – chapter numbering.** A split-off fragment becomes a real chapter titled "Chapter {index}" (`ocr_build.py:113`). It gets a guessed chapter number from its position (`chapter_numbers.py:349-360`). In the example of chapters 1, 2, a fragment, then chapter 3, the fragment is guessed as 3 and collides with the real chapter 3. If no chapter states a number, every later chapter shifts by one. These numbers flow into the document export's chapter titles (`docexport.py:61-77`) and into posting.

## Candidates refuted
- **PhonePage.jsx:** it only handles posting and never uploads photos.
- **Reading order:** each page is read on its own with no neighbouring text (`ocr.py:257-273`). Results are saved by page id under a lock (`app.py:4880-4898`). Read jobs are keyed by page number plus job type (`app.py:4496`), so they can't be confused with chapters.
- **Two writers scrambling the page list:** everything that saves the page list loads it fresh under a lock and updates pages by id. Only reorder and delete change the list's order (`pages.py:177-187`).
- **Order within a single drop:** the browser waits for each photo before sending the next, and the server adds each one to the end in that order (`PagesPage.jsx:172-183`). The file input is cleared only after the files are copied (`:165`, `:337`).
- **Reorder check:** a partial or unknown page list is refused (`pages.py:275`). If an upload races a reorder, you get a visible error and a reload, not lost pages.
- **Claude's join answers landing on the wrong page:** answers are checked to be in range and not repeated (`ocr.py:234`).
- **Deleting renumbers pages:** page numbers and file names never change. The next refresh correctly removes a deleted page's text.
- **Exports reading page order:** the document, Google Docs and EPUB exports never read `pages.json` or the chapter's photo list. They follow chapter order only (the index problems above still apply).
- **Bundle export and import:** `pages.json` is carried over unchanged (`projects.py:229-293`).
- **Something else rewriting `source.json`:** for photo novels only the build endpoint writes it (`app.py:1757`), and appended chapters line up with their photo lists.
- **Heading, separator and single modes ignoring batches:** that is by design. Their own problems are covered in #14.

## What this means for the chapter-box redesign
- **Explicit chapters:**
  - Group pages by chapter-box id, in box order and in order within each box, rather than by runs of neighbouring pages.
  - Add an endpoint to create an empty box and one to move pages between boxes.
  - The "+" tile should upload into its own box and insert after that box's last page.
- **Uploads:**
  - Put files in a set order before sending (natural name order by default) and show that order.
  - Send uploads one drop at a time so drops can't interleave.
  - When one file fails, keep going or retry into the same box instead of stopping the whole drop.
- **Building:**
  - A built chapter should be rebuilt from its box's current pages, in current order, and its photo list updated.
  - Photos added to a built box should go into that chapter, not become a new one.
  - Building a box with unapproved or empty pages should refuse or clearly warn, not quietly leave pages out.
- **Joins:** reset automatic joins and flag hand-set joins whenever a page's neighbour changes.
- **UI fixes:**
  - Show the build warnings.
  - Clear the drag state when a drag ends.
  - Treat dropped files separately from rows dragged inside the list.

