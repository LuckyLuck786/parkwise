import { useMemo, useState } from 'react'

import type { Bay, BayState } from '@/types'

interface BayGridProps {
  bays: Bay[]
  selectedLot: string
  onSelectBay?: (bay: Bay) => void
}

interface StateStyle {
  fill: string
  stroke: string
  label: string
  /** Drawn inside the bay so state is never conveyed by colour alone. */
  pattern: 'none' | 'hatch' | 'dots' | 'cross'
}

export const BAY_STATE_STYLES: Record<BayState, StateStyle> = {
  free: { fill: '#dcfce7', stroke: '#22c55e', label: 'Free', pattern: 'none' },
  allotted: { fill: '#fef3c7', stroke: '#f59e0b', label: 'Allotted', pattern: 'hatch' },
  occupied: { fill: '#1e293b', stroke: '#0f172a', label: 'Occupied', pattern: 'dots' },
  blocked: { fill: '#fee2e2', stroke: '#dc2626', label: 'Blocked', pattern: 'cross' },
  unknown: { fill: '#e2e8f0', stroke: '#94a3b8', label: 'Unknown', pattern: 'hatch' },
}

const MARGIN = 12
const SCALE = 3.2

export function BayGrid({ bays, selectedLot, onSelectBay }: BayGridProps) {
  const [hovered, setHovered] = useState<Bay | null>(null)

  const { paths, width, height } = useMemo(() => {
    // Bays are laid out in campus metres; scale to SVG user units.
    const xs = bays.map((b) => b.x)
    const ys = bays.map((b) => b.y)
    const minX = Math.min(...xs, 0)
    const maxX = Math.max(...xs.map((x, i) => x + bays[i].width), 1)
    const minY = Math.min(...ys, 0)
    const maxY = Math.max(...ys.map((y, i) => y + bays[i].height), 1)

    return {
      paths: bays.map((bay) => {
        const style = BAY_STATE_STYLES[bay.state] ?? BAY_STATE_STYLES.unknown
        const px = (bay.x - minX) * SCALE + MARGIN
        const py = (bay.y - minY) * SCALE + MARGIN
        return {
          bay,
          style,
          x: px,
          y: py,
          w: Math.max(6, bay.width * SCALE - 3),
          h: Math.max(6, bay.height * SCALE - 3),
        }
      }),
      width: (maxX - minX) * SCALE + MARGIN * 2,
      height: (maxY - minY) * SCALE + MARGIN * 2,
    }
  }, [bays])

  const describedBy = hovered ? `bay-desc-${hovered.id}` : undefined

  return (
    <div className="space-y-2">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full rounded-xl bg-slate-50 ring-1 ring-slate-200"
        role="img"
        aria-label={`Bay layout for ${selectedLot}, ${bays.length} bays`}
      >
        <defs>
          <pattern id="pw-hatch" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <line x1="0" y1="0" x2="0" y2="6" stroke="#f59e0b" strokeWidth="2" opacity="0.5" />
          </pattern>
          <pattern id="pw-dots" width="5" height="5" patternUnits="userSpaceOnUse">
            <circle cx="2.5" cy="2.5" r="1.1" fill="#94a3b8" opacity="0.55" />
          </pattern>
          <pattern id="pw-cross" width="8" height="8" patternUnits="userSpaceOnUse">
            <path d="M0 0 L8 8 M8 0 L0 8" stroke="#dc2626" strokeWidth="1.5" opacity="0.5" />
          </pattern>
        </defs>

        {paths.map(({ bay, style, x, y, w, h }) => (
          <g key={bay.id}>
            <rect
              x={x}
              y={y}
              width={w}
              height={h}
              rx={2}
              fill={style.fill}
              stroke={style.stroke}
              strokeWidth={bay.reserved_tier > 0 ? 2 : 1}
              strokeDasharray={bay.reserved_tier > 0 ? '4 2' : undefined}
              tabIndex={0}
              role="button"
              aria-label={`${bay.label}, ${style.label}${bay.is_accessible ? ', accessible' : ''}${bay.reserved_tier > 0 ? `, tier ${bay.reserved_tier} reserved` : ''}`}
              aria-describedby={describedBy}
              onMouseEnter={() => setHovered(bay)}
              onMouseLeave={() => setHovered(null)}
              onFocus={() => setHovered(bay)}
              onBlur={() => setHovered(null)}
              onClick={() => onSelectBay?.(bay)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  onSelectBay?.(bay)
                }
              }}
              className="cursor-pointer transition hover:brightness-95"
            />
            {/* Reserved bays get a corner marker, not just a dashed outline. */}
            {bay.reserved_tier === 1 && (
              <text x={x + 3} y={y + 9} fontSize="7" fill="#0f766e" fontWeight="700">
                ♿
              </text>
            )}
            {bay.reserved_tier === 2 && (
              <text x={x + 3} y={y + 9} fontSize="7" fill="#7c2d12" fontWeight="700">
                T2
              </text>
            )}
          </g>
        ))}
      </svg>

      <p id={describedBy} className="min-h-[18px] text-[11px] text-slate-600" aria-live="polite">
        {hovered ? (
          <>
            <strong>{hovered.label}</strong> — {(BAY_STATE_STYLES[hovered.state] ?? BAY_STATE_STYLES.unknown).label}
            {hovered.is_accessible ? ', accessible bay' : ''}
            {hovered.reserved_tier > 0 ? `, tier ${hovered.reserved_tier} reserved` : ''}
            {hovered.bay_type !== 'any' ? `, ${hovered.bay_type.replace('_', ' ')} only` : ''}
            {hovered.low_confidence ? ' — low confidence (sensor offline, state inferred from scans)' : ''}
          </>
        ) : (
          'Hover or focus a bay for its state.'
        )}
      </p>
    </div>
  )
}
