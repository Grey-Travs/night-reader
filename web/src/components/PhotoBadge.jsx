// The badge on a photo's thumbnail while its chapter is being shot (see photoBadge.js).
export default function PhotoBadge({ badge }) {
  if (!badge) return null
  return (
    <span className={`camera-badge camera-badge-${badge.tone}`} title={badge.long}>
      {badge.short}
    </span>
  )
}
