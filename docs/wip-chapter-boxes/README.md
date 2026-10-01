# Chapter boxes on the Pages tab (work in progress)

This folder is only for handing the work over between computers. Delete it before merging
into `main`.

## Where things stand (2026-10-01)
- The plan is approved: [plan.md](plan.md). Where the documents disagree,
  [contract.md](contract.md) wins, then the plan, then [critique.md](critique.md), then the two
  design docs.
- The code is about half way through the build. Two agents were writing it in parallel (one
  for `server/` + `tests/`, one for `web/`). They were paused before they had checked their
  own work, so nothing has been reviewed yet.
- Test results at the pause:
  - Backend: 1181 passed (`.venv\Scripts\python.exe -m pytest tests -q`).
  - Web: 259 passed, 1 failed (`npm test` in `web/`). The failing test is
    `PagesPage.test.jsx › the photo view › shows the text as soon as Claude finishes reading
    the open photo`.
- Still to do:
  1. Finish the implementation against the plan. That includes the failing test above and a
     line-by-line check of both halves against `contract.md`.
  2. Run the review from `implement-workflow.js`: the contract, backend and frontend
     reviewers. Then fix what they find.
  3. Run the verification in the plan, including trying the real app with test photos.

## Picking it up on another computer
1. `git fetch` and then `git checkout pages-chapter-boxes`.
2. Run `setup.bat`. It installs the Python and web packages.
3. Install pytest, which is only used for the tests:
   `.venv\Scripts\python.exe -m pip install pytest`
4. Copy over the files git doesn't carry: `client_secret.json`, `token.json`, `config.toml`
   and the `projects/` folder, if you want your novels there too.
5. Ask Claude to carry on from this folder.

`implement-workflow.js` holds absolute paths from the first computer. Point them at this
folder before running it again.
