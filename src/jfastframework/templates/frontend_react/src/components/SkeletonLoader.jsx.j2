const SHAPES = {
  text: 'h-4 rounded',
  block: 'h-24 w-full rounded-lg',
  circle: 'h-10 w-10 rounded-full',
}

export default function SkeletonLoader({ lines = 3, shape = 'text', className = '' }) {
  const shapeClass = SHAPES[shape] ?? SHAPES.text

  return (
    <div role="status" className={`space-y-3 ${className}`}>
      <span className="sr-only">Loading…</span>
      {Array.from({ length: lines }, (_, index) => (
        <div
          key={index}
          aria-hidden="true"
          // animate-pulse is not covered by prefers-reduced-motion on its own,
          // and an indefinite loop is exactly what that setting exists to stop.
          className={`animate-pulse bg-elevated motion-reduce:animate-none ${shapeClass} ${
            shape === 'text' ? (index === lines - 1 ? 'w-2/3' : 'w-full') : ''
          }`}
        />
      ))}
    </div>
  )
}
