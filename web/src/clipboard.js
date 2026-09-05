// Plain-text copy with the same three-step fallback the chapter reader uses for rich
// text: the async Clipboard API, then writeText, then a hidden textarea + execCommand.
// The last step matters because clipboard permissions can be denied outright, and a
// "Copy report" button that silently does nothing is worse than no button.
export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* fall through */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch {
    return false
  }
}
