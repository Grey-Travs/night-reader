# Web-Novel Translator

A local app that translates Korean web novels into natural, native-English
web-novel prose using Claude — with consistent character names, automatic quality
checks, and a clean reading view. You keep a **library** of novels: paste a Google
Doc, and the app reads it, translates it chapter by chapter, and saves clean output.

> **Runs on your own Claude subscription** (Max/Pro) via the Claude Agent SDK — no
> API key, no per-word charges. Everything runs locally on your computer; your
> documents never leave your machine except to Claude for translation.

---

## What it does

- **Library of novels.** Paste a Google Docs link (one chapter per tab). The app
  reads the doc, names the project from its title, and tracks each one separately.
- **Faithful translation.** Whole-chapter calls preserve voice and honorifics;
  fidelity is the top priority (no omissions, no padding).
- **Name/term consistency.** A per-novel glossary locks character names and terms so
  spellings never drift across chapters. New names are queued for your approval.
- **Automatic quality checks.** Every chapter is validated (length, structure,
  dialogue, formatting). Suspect output is flagged "needs review," never silently
  accepted.
- **Mixed documents handled.** If some tabs are already in English, the app detects
  and skips them — it only translates the Korean ones.
- **Resumable.** When your plan's usage limit is reached, translation pauses and
  resumes later; finished chapters are never redone.

---

## How it works

```
React app (browser)  ──►  FastAPI backend  ──►  translation engine
  library / reader          (local server)         │
                                                    ├─ Google Docs API (reads your doc)
                                                    └─ Claude Agent SDK (your subscription)
```

Each novel lives in `projects/<id>/` with its own glossary, progress, and output
files. Shared settings and logins live in `config.toml` / your Claude + Google logins.

---

## Requirements

- **Python 3.11+** and **Node.js 18+**
- A **Claude Max or Pro subscription**, logged in via Claude Code (this app uses that
  login — it does **not** use a paid API key)
- A **Google account** that owns the novel documents

---

## Setup

### Windows (easy path)

1. **Install dependencies** — double-click **`setup.bat`** (creates the Python
   environment and installs everything).
2. **Google access** — create a free Google OAuth "Desktop app" credential and save
   it as `client_secret.json` in this folder. See
   [Google setup](#google-setup) below.
3. **Claude** — make sure you're logged into Claude Code (you already are if Claude
   Code works for you).
4. **Run** — double-click **`start.bat`**. Your browser opens to the app.

### macOS / Linux (or manual)

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd web && npm install && cd ..
# add client_secret.json (see Google setup), then:
.venv/bin/python launch.py
```

### Google setup

1. In the [Google Cloud Console](https://console.cloud.google.com/): create a project
   and **enable the Google Docs API**. (Drive is not needed.)
2. Configure the **OAuth consent screen** (External) and add yourself as a **test user**.
3. **Credentials → Create credentials → OAuth client ID → Desktop app**, then
   **Download JSON** and save it here as `client_secret.json`.
4. The first time you translate, a browser window asks you to grant **read-only**
   access to your Google Docs. (You may see an "unverified app" screen — that's normal
   for a personal app; choose Advanced → continue.)

---

## Using the app

1. **Add a novel** — paste its Google Docs link. The app reads it and lists the chapters.
2. **Translate** — click *Translate all remaining*, or translate single chapters.
   Watch progress live as each chapter completes.
3. **Review the glossary** — approve, edit, or reject newly found names so they stay
   consistent everywhere.
4. **Read** — click any chapter to read the English (toggle the Korean source
   side-by-side). Files are saved as Markdown in `projects/<id>/chapters/`.

> **Usage limits:** translation draws on your Claude plan's allowance (no paid
> overflow). When the limit is hit, it pauses; come back later and continue — nothing
> is re-done or re-charged.

---

## Command-line (optional)

The engine also works headlessly without the app:

```bash
.venv/bin/python -m translation_bot run        # translate (single-project mode)
.venv/bin/python -m translation_bot status
```

See [translation_bot/](translation_bot/) for the engine and `config.example.toml` for
all tunables (model, effort, validation thresholds, chunking, honorific rules).

---

## Tech

- **Engine:** Python — Google Docs API extraction, Claude Agent SDK translation,
  glossary, validation, resumable state.
- **Backend:** FastAPI (serves the API and the built interface; streams live progress
  over Server-Sent Events).
- **Frontend:** React + Vite + Tailwind CSS.

## Privacy & cost

Everything runs locally. Your documents are sent only to Claude (for translation) and
read from Google Docs (your own account). There is no server you don't control. Cost is
your existing Claude subscription — the app shows a *plan-usage equivalent* figure for
visibility, but you are not billed per translation.

## License

[MIT](LICENSE). Contributions welcome.
