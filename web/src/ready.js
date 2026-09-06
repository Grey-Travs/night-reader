// What the app genuinely needs before it can do anything: its config, and Claude.
//
// Google is deliberately NOT in here. It is only ever used to READ a Google Doc — a
// novel from pasted text or from page photos never touches it. Gating the whole app
// on it walled OCR-only users behind an OAuth consent screen they had no use for.
// The sidebar still shows a grey Google dot, and Setup is one click away, so anyone
// who does want Google Docs can still see that it isn't connected.
//
// This lives in its own module because App.jsx and SetupWizard.jsx each used to carry
// their own copy. When one was relaxed and the other wasn't, the wizard's only exit
// button stayed disabled forever and a Google-less user was trapped in a full-screen
// dialog with no Skip, no Esc and no back — reachable from Settings -> "Run full setup".
export function isReady(status) {
  return Boolean(status && status.config_present && status.claude_logged_in)
}

// Everything connected, Google included. Only for telling the user what is still
// outstanding — never for gating access to the app.
export function isFullyConnected(status) {
  return Boolean(
    isReady(status) && status.google_client_secret_present && status.google_logged_in,
  )
}
