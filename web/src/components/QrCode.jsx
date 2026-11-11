import { useMemo } from 'react'
import { qrcode } from 'qrcode-generator'

// A QR code, drawn as SVG squares from the library's grid of modules rather than its own
// SVG string, so nothing reaches the page as raw HTML. Black on white with a four-module
// margin whatever the theme: a phone camera reads a dark code on a light ground.
export default function QrCode({ text, size = 220, label = 'QR code' }) {
  const grid = useMemo(() => {
    const qr = qrcode(0, 'M')
    qr.addData(text)
    qr.make()
    const n = qr.getModuleCount()
    let d = ''
    for (let r = 0; r < n; r += 1) {
      for (let c = 0; c < n; c += 1) if (qr.isDark(r, c)) d += `M${c} ${r}h1v1h-1z`
    }
    return { n, d }
  }, [text])
  return (
    <svg
      role="img" aria-label={label} width={size} height={size}
      viewBox={`-4 -4 ${grid.n + 8} ${grid.n + 8}`} shapeRendering="crispEdges"
      style={{ background: '#ffffff', borderRadius: 4 }}
    >
      <path d={grid.d} fill="#000000" />
    </svg>
  )
}
