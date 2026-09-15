# Night Reader poster

Posts finished chapters to meiko.studio from inside **your own** Chrome, using the account
you are already signed into.

## Why an extension and not automation from outside

Since Chrome 136, `--remote-debugging-port` is ignored on your default profile, and a
copied profile gets a different encryption key so its cookies will not decrypt. There is
therefore no way to drive your everyday Chrome session from outside it. An extension runs
*inside* that session: no second login, no debug port, no flags, and nothing extra to keep
running.

## Install (once)

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and pick this `extension/` folder.
4. Make sure Night Reader itself is running (`python launch.py`).

Open any meiko.studio page and a small panel appears bottom-right. It says **connected**
when it can reach Night Reader on `localhost:8000`.

## What it can do right now

Both of these are **read-only**: they create nothing, submit nothing, and leave nothing
behind to delete. That is deliberate — removing a chapter on meiko means retyping its name
to confirm, so anything that left an orphan chapter per attempt would be painful.

**Probe page** — reports every button, dropdown, input, toggle and table row it can
actually see, with a suggested CSS path for each. Press **Copy report** and hand it back;
that is what the selectors in `adapters/meiko.toml` get corrected from, instead of being
guessed off screenshots.

**Test paste** — the one genuinely uncertain step. Open **any chapter that already
exists** → Pages → Write Page, then press this. It pastes two sample paragraphs into the
editor and reports whether the character counter moved off zero. Then **Close** the editor
without submitting. If the counter moved, the synthetic paste works and the posting loop
can be built on it; if not, the fallback is the real clipboard plus a genuine Ctrl+V.

## How the pieces fit

- `content.js` — the only part that touches the page. Finds things by their visible text,
  because text survives a redesign where a generated class name does not.
- `background.js` — the only part that talks to Night Reader. It lives in the service
  worker because MV3 grants that cross-origin access via `host_permissions`, so the app's
  API needs no CORS change; the same fetch from a content script would have.
- The selectors live in `adapters/meiko.toml` **in the app**, served at
  `/api/posting/adapter`. When the site moves a button you edit one line there and reload
  the page — no code change, no extension reload.

## Notes

- The extension only acts while this Chrome is open on the machine.
- It never presses **Send Notification**.
- Automated posting may be against meiko.studio's terms of service. The account risk is
  yours.

## Reloading after a change

Two steps, and the second is easy to miss:

1. `chrome://extensions` -> the reload arrow on this extension.
2. **Refresh the meiko.studio tab.** Content scripts are injected on page load, so an
   already-open tab keeps running the OLD code no matter how many times the extension is
   reloaded.

Skipping step 2 cost a full debugging round: a fix that had shipped looked like a fix that
had failed, because the reports came from the previous build. Every report now opens with
`[extension v...]` so the build that produced it is never in doubt.

## Version

The panel shows the loaded version in its header. An unpacked extension does **not** reload
itself, so a change can look like it did nothing when the old code is simply still running —
check the number in the panel against the one below before concluding a fix failed.

Bump `manifest.json` on every change, and note it here.

| Version | What changed |
|---|---|
| 0.19.0 | **`pages=true` is how a chapter's text is read.** Four guesses at `uid` / `id` / `chapter_uid` all came back with a chapter's settings and no prose; the parameter appeared in the log the moment a chapter's editor was opened by hand — the same lesson as the Create button, that the page knows how to ask so it is cheaper to watch it ask than to guess. Fetch one chapter now asks the one question left: whether `pages=true` works on the LIST as well as on a single chapter, which is the difference between reading a 150-chapter novel in one request and in 150. It surveys the tag, attribute and entity inventory across *every* chapter rather than sampling the first, because the one chapter somebody formatted by hand is exactly the one a sample misses. Also fixes a wrong conclusion in 0.18.0: it announced "the list DOES carry the text" on the strength of one record having text, when a record only carries text if it was fetched with `pages=true` — so that record was simply the one whose editor had been opened. It now reports the count and declines to conclude anything from it. **And a real pricing bug:** a FREE chapter still carries a coin value (`paid: 0` alongside `coins: 1`, observed), so reading the coins box out of the row had a mostly-free series priced at 1 coin. Prices now come from the site's own chapter records, which draw the distinction the box does not, and a row whose record has not been seen contributes nothing rather than a guess. |
| 0.18.0 | **Reads the site well enough to import from it.** Show reads now groups by endpoint instead of printing six near-identical blocks, and — the fix that mattered — tells a chapter record apart from the studio and series ones. `reportChapters` matches by shape, so anything with an `id` and a `displayName` lands in `CHAPTERS`, and 0.17.0 duly reported the *studio* record as its sample chapter; the count was the tell, 7 records for a 5-chapter novel being 1 studio + 1 series + 5 chapters. Classified now by `plan`/`storage`, `slug`, and `series_uid` + `type`. It also lists every field on a real chapter, any field long enough to be prose whatever it is called, and the tag, attribute and entity inventory of the markup — which for a novel typed into the site's own editor is not the eight attribute-free tags this app emits. New **Fetch one chapter**: the list turned out not to carry the text, so this asks for one chapter four different ways and reports which comes back with prose. Four read-only GETs against the account's own data, behind their own button because Show reads promises to touch nothing. |
| 0.17.0 | **Show reads** — the read side of the API, which nothing had ever looked at. Every other diagnostic here filters GETs out on purpose, because when the question was "did my click send anything" the GETs were the page refreshing itself and they buried the request that mattered. That filter turned out to be the only reason the read API was unknown: `keepBody` has been keeping `/app/` GET bodies all along and `reportChapters` has been filling `CHAPTERS` with the site's own records, unclipped. This reports which request fetches the chapter list and with what parameters, whether that list carries the chapter TEXT (one request per novel) or does not (one per chapter), and the tag and entity inventory of the real HTML — which for a novel typed into the site's own editor is whatever that editor emits, not the eight tags Night Reader produces. Also adds `unb64` and `chapterHtml`: the inverse of `b64` had never existed, and bare `atob` returns bytes, so every curly quote in a chapter would have arrived as mojibake. Writes nothing. |
| 0.16.0 | **Re-identifies the series when the route changes.** `match` was asked exactly once, in `build()`, which runs at `document_idle` and never again — and meiko is a Nuxt app, so clicking from the studio list into a novel changes the URL with no page load. Entering on the studio list (the normal entry point, which correctly matches no series) therefore pinned the panel to a red "no series for this page" for the whole session however many novels were opened, and because the claim poll was gated on that first answer it never started at all. Now polled once a second against `location.href`: `popstate` covers only back and forward, and hooking `history.pushState` has to happen in the page's own world, which this script is not in. The status line carries the URL it tried, so a future mismatch says why. A run also pins its series at the start and stops rather than create a chapter under the wrong novel, because `uidsFromUrl` reads the current path on every create. |
| 0.15.0 | **Takes posting runs from Night Reader.** Until now these buttons were the only way to start posting: the app could show what a run would do but never begin one. The app now writes the request to a file and this asks for it every 30 seconds, so a run can be started from the Posting page — or from a phone, since the request waits on disk rather than in the tab that asked for it. Gated on the page actually having the chapter list, because the server hands a queued run out once and a tab sitting on a single chapter would take it and then fail for want of the list it needs to avoid double-posting. Also pushes the chapter list and the coin prices it reads there back to the app, which is how the plan learns what is already published and what a series charges — neither of which Python can see. |
| 0.14.0 | **Range and publishing.** "up to ch N" names the finishing line instead of a count, so a run can be repeated without working out how many are left (side stories, which have no global number, are never swept up by a numeric range). **Publish private** flips the chapters Night Reader posted from private to public — re-sending the same HTML with the state change rather than the state alone, because the site's own save always carries a `pages` body and a PUT without one could be read as "no pages", which would blank already-published chapters. Idempotent: anything already public is skipped. The site's chapter records are learned from the list the page fetches for itself, since a chapter can only be updated by PUTting its whole record back. |
| 0.13.0 | **Batch posting.** A count box plus **as Private** / **as Public**, posting serially and stopping at the first thing that is not exactly right — whatever is wrong with one chapter is likely wrong with the next thirty, and a batch that ploughs on leaves a mess that has to be undone by retyping chapter names. Every run ends with a list of exactly what went out, which matters because posting via the API leaves the site's own list stale until a refresh. New **Posted so far** reads the durable ledger. An expired session is now named as such rather than reported as a refusal. 1.5s between chapters, so a burst of writes does not look like something worth rate-limiting. |
| 0.12.0 | The page now refreshes itself after a post. Posting through the API leaves the site's own chapter list stale — Vue fetched it before the chapter existed — so a newly posted chapter did not appear until the tab was reloaded by hand. A list that silently lags reality is also how a chapter gets posted twice. The report is kept in sessionStorage and restored afterwards, so the reload does not throw it away. |
| 0.11.1 | The API path worked on its first real run — chapter 69 was created with its text, `type=notes`, 55 coins — and then my own check failed it. Two bugs in one line: `state` was sent capitalised (the site's own requests use lowercase, and the server stores what it is given verbatim), and the echoed value was compared against a lowercased copy. Verification is now case- and type-tolerant, since the server also normalises `paid: true` to `1`. Added a **resume record**: a chapter created but not finished is remembered and continued into, rather than being created a second time — which matters because removing either copy means retyping its name. |
| 0.11.0 | **Posts through the site's own API instead of clicking.** The captured requests showed the API lives on `hirayatales.com/app/chapter` — and that our failing request was going to `/app/studio`, because the page has TWO buttons labelled "Create" and the sidebar one is studio-scoped. Both open an identical Name dialog, so nothing in the DOM distinguished them. Posting is now two requests: `POST /app/chapter` to create, then one `PUT` carrying the settings and the text together — so there is no window where an empty chapter is public. The settings are verified from the server's own reply. Content is base64'd UTF-8-safe (`btoa` alone throws on the curly quotes these translations use). The token stays in the page's context and is never logged. |
| 0.10.0 | New **API shape** button: breaks each write out host, path and parameter by parameter, decoding base64 values inline. This site's API takes everything in the query string, so a successful request is a template — swap the chapter name and re-issue it. That is a far better layer to automate than the DOM: no Vue state to update, no disabled buttons, no dialog focus trap, no timing. Building it needs the real request param by param, which a clipped URL could not give. |
| 0.9.1 | **Fixed a self-inflicted blind spot.** The credential fix in 0.7.0 filtered the netlog to same-origin requests, but this site's API is not served from the page's host — so the one request under investigation was being discarded silently, and two capture rounds came back showing zero writes. Now only known auth and analytics hosts are excluded, and the log keeps the HOST in the URL (it is not a secret, and hiding it is what stopped a cross-domain API from being recognised as the site's own). |
| 0.9.0 | Replaced the arm-then-act **Watch create** button with a **live write counter** in the panel header, visible even when folded. Two buttons that had to be pressed in the right order was a poor design — each cleared the other's output, and a report taken at the wrong moment was indistinguishable from one where nothing was sent. Nothing needs arming now: fold the panel, do the thing, watch the number change. |
| 0.8.1 | The panel can be **dragged by its header** and **collapsed** to a single bar, with both remembered. It was parked over the site's own Submit and Save — a floating panel wants the same corners those buttons occupy — which made the very thing being asked for impossible to click. Position is clamped back on-screen so it cannot be dragged somewhere unreachable. |
| 0.8.0 | New **Watch create** button: arm it, then do the action by hand, and the request prints itself. Asking someone to act and then press a button leaves the ordering to chance, and a report taken too early looks identical to one where the action sent nothing — which is exactly what happened. Also: GET response bodies are no longer recorded (this site's icon endpoints returned pages of JSON that buried the one request that mattered), and **Show requests** now says plainly when there are no writes rather than dumping every GET. |
| 0.7.1 | Every report now stamps `[extension v...]` in its first line, so a log can always be matched to the build that produced it. A report from a stale content script is otherwise indistinguishable from a current one, and reading one as the other wasted a round. Reload instructions above now say to refresh the tab too. |
| 0.7.0 | **Security fix.** The netlog printed a Firebase `refresh_token` in full — a long-lived credential, and not JWT-shaped, so the previous redactor missed it. It now records **only this site's own requests**: auth (Google) and telemetry (Cloudflare) are dropped before they are even summarised, which removes the whole class of leak and the noise with it. Key-name redaction widened as a second layer, and emails are masked. The log also survives a page reload now — creating a chapter navigates, and the first attempt to capture a manual Create lost the evidence to that. |
| 0.6.0 | **Fixed a double-click.** `realClick` was dispatching a synthetic click AND calling `el.click()`, firing the site's handler twice — two identical create requests a millisecond apart. Both failed so it went unnoticed, but a success would have made two chapters. Also: netlog now **redacts auth tokens** (this site puts a Firebase JWT carrying the account email in the query string, so an unredacted log leaks a live credential) and records the REQUEST body as well as the response. New **Show requests** button, for capturing a manual Create to compare against. |
| 0.5.0 | Added `netlog.js`, which runs in the PAGE's own context (`world: "MAIN"`) and records every fetch/XHR the site makes. Three rounds of DOM debugging could not tell "the click never reached the handler" from "the handler ran and the server refused" — the network can. On a failed submit the run now prints the requests made, their status and response body. Read-only: it never alters a request. |
| 0.4.0 | Dropped the synthetic `blur` from text entry — a Headless UI dialog traps focus and can treat focus leaving as a dismissal, so the dialog may have been gone before Submit was clicked, which looks identical to a handler doing nothing. The run now asserts the dialog is still open before submitting, checks `aria-disabled` as well as `disabled`, and escalates click -> `form.requestSubmit()` -> Enter, reporting which one worked and listing the dialog's buttons if none did. |
| 0.3.0 | Text is entered with `execCommand('insertText')`, the one programmatic route that makes the browser itself emit `beforeinput`/`input` — so the component's state actually updates. Assigning `.value` had put "Chapter 62" on screen while the site submitted an empty name, and neither checking `.value` nor checking whether Submit was enabled could catch it (that Submit is enabled with the field empty). The run now also reports which method entered the text, and reads back whatever notification the site shows after Submit. |
| 0.2.0 | Submit is looked up **inside the dialog panel**. Searching the whole page found a same-named button outside it, and clicking that counted as a click-away: the dialog closed, nothing was created, and the log still said Submit was clicked. Plus: full pointer/mouse click sequence (a bare `el.click()` never opened the dialog at all), `typeInto` so Vue registers the name rather than just the DOM showing it, no clicking disabled controls by default, the URL-change wait that separates "never created" from "form looks different", and the version shown here. |
| 0.1.0 | First version: page probe, paste test, and the 8-step posting loop. |
