import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Gauge, MapPinned, RefreshCw } from 'lucide-react'

import { BayGrid } from '@/components/BayGrid'
import { apiError, fetchHealth, fetchLots } from '@/services/api'
import type { Bay, HealthResponse, LotsResponse } from '@/types'

export default function App() {
  const [lots, setLots] = useState<LotsResponse | null>(null)
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [selectedLot, setSelectedLot] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [selectedBay, setSelectedBay] = useState<Bay | null>(null)

  const reload = useCallback(async () => {
    try {
      const data = await fetchLots()
      setLots(data)
      setSelectedLot((current) => current ?? data.lots[0]?.lot.id ?? null)
      setError(null)
    } catch (err) {
      setError(apiError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
    void fetchHealth().then(setHealth).catch(() => undefined)
    // Polling fallback for the live view. Phase 5 replaces this with SSE plus
    // the same poll when the stream is unavailable.
    const timer = window.setInterval(() => void reload(), 5000)
    return () => window.clearInterval(timer)
  }, [reload])

  const active = useMemo(
    () => lots?.lots.find((entry) => entry.lot.id === selectedLot) ?? lots?.lots[0] ?? null,
    [lots, selectedLot],
  )

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-4 p-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-slate-900">ParkWise</h1>
          <p className="text-xs text-slate-600">
            Priority-based campus parking · {health ? `${health.database} · ${health.environment}` : 'connecting…'}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void reload()}
          className="inline-flex items-center gap-1.5 rounded-xl bg-white px-3 py-2 text-xs font-semibold text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50"
        >
          <RefreshCw className="h-4 w-4" aria-hidden /> Refresh
        </button>
      </header>

      {error && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-sm font-semibold text-red-800 ring-1 ring-red-300">
          {error}
        </p>
      )}

      {lots?.simulated && (
        <p className="flex items-start gap-2 rounded-xl bg-amber-50 px-3 py-2 text-xs font-semibold text-amber-900 ring-1 ring-amber-300">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          Simulated data — {lots.note}
        </p>
      )}

      {loading && <p className="text-sm text-slate-600">Loading lots…</p>}

      {lots && (
        <>
          <section aria-label="Lots" className="grid gap-3 sm:grid-cols-3">
            {lots.lots.map((entry) => {
              const { lot } = entry
              const pct = Math.round(lot.fill_ratio * 100)
              return (
                <button
                  key={lot.id}
                  type="button"
                  onClick={() => setSelectedLot(lot.id)}
                  aria-pressed={active?.lot.id === lot.id}
                  className={`rounded-2xl bg-white p-3 text-left shadow-panel ring-1 transition ${
                    active?.lot.id === lot.id ? 'ring-2 ring-campus-600' : 'ring-slate-200'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-bold text-slate-900">{lot.name}</span>
                    <span className="font-mono text-xs text-slate-600">{pct}%</span>
                  </div>
                  <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-slate-200">
                    <div
                      className={`h-full rounded-full ${
                        pct >= 90 ? 'bg-red-500' : pct >= 70 ? 'bg-amber-500' : 'bg-emerald-500'
                      }`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <dl className="mt-2 grid grid-cols-3 gap-1 text-[11px] text-slate-600">
                    <div>
                      <dt className="sr-only">Free</dt>
                      <dd>
                        <span className="font-bold text-emerald-700">{lot.free_bays}</span> free
                      </dd>
                    </div>
                    <div>
                      <dt className="sr-only">Occupied</dt>
                      <dd>
                        <span className="font-bold text-slate-900">{lot.occupied_bays}</span> occ
                      </dd>
                    </div>
                    <div>
                      <dt className="sr-only">Accessible</dt>
                      <dd>
                        <span className="font-bold text-teal-700">{lot.accessible_bays}</span> ♿
                      </dd>
                    </div>
                  </dl>
                </button>
              )
            })}
          </section>

          {active && (
            <section className="rounded-2xl bg-white p-4 shadow-panel ring-1 ring-slate-200" aria-label="Bay map">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <h2 className="flex items-center gap-2 text-sm font-bold text-slate-900">
                  <MapPinned className="h-4 w-4" aria-hidden />
                  {active.lot.name}
                </h2>
                <span className="text-[11px] text-slate-500">
                  {active.bays.length} bays · generated {new Date(lots.generated_at).toLocaleTimeString()}
                </span>
              </div>
              <BayGrid bays={active.bays} selectedLot={active.lot.id} onSelectBay={setSelectedBay} />
            </section>
          )}

          <section className="grid gap-3 sm:grid-cols-2" aria-label="Legend and selection">
            <div className="rounded-2xl bg-white p-4 shadow-panel ring-1 ring-slate-200">
              <h2 className="mb-2 flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-slate-500">
                <Gauge className="h-4 w-4" aria-hidden /> Legend
              </h2>
              <ul className="grid grid-cols-2 gap-1.5 text-[11px] text-slate-700">
                {(['free', 'allotted', 'occupied', 'blocked', 'unknown'] as const).map((state) => (
                  <li key={state} className="flex items-center gap-1.5">
                    <span
                      aria-hidden
                      className="h-3.5 w-3.5 rounded border"
                      style={{
                        background:
                          state === 'occupied' ? '#1e293b' : state === 'free' ? '#dcfce7' : state === 'allotted' ? '#fef3c7' : state === 'blocked' ? '#fee2e2' : '#e2e8f0',
                        borderColor:
                          state === 'occupied' ? '#0f172a' : state === 'free' ? '#22c55e' : state === 'allotted' ? '#f59e0b' : state === 'blocked' ? '#dc2626' : '#94a3b8',
                      }}
                    />
                    <span className="capitalize">{state}</span>
                  </li>
                ))}
                <li className="flex items-center gap-1.5">
                  <span aria-hidden>♿</span>
                  <span>tier 1 accessible</span>
                </li>
                <li className="flex items-center gap-1.5">
                  <span aria-hidden>T2</span>
                  <span>tier 2 reserved</span>
                </li>
              </ul>
              <p className="mt-2 text-[11px] text-slate-500">
                Dashed outline means reserved. Patterns repeat inside each bay so state is legible without
                colour.
              </p>
            </div>

            <div className="rounded-2xl bg-white p-4 shadow-panel ring-1 ring-slate-200">
              <h2 className="mb-2 text-xs font-bold uppercase tracking-wide text-slate-500">Selected bay</h2>
              {selectedBay ? (
                <dl className="space-y-1 text-xs text-slate-700">
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Label</dt>
                    <dd className="font-mono font-bold">{selectedBay.label}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">State</dt>
                    <dd className="font-semibold capitalize">{selectedBay.state}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Type</dt>
                    <dd className="capitalize">{selectedBay.bay_type.replace('_', ' ')}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Accessible</dt>
                    <dd>{selectedBay.is_accessible ? 'Yes' : 'No'}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Reserved tier</dt>
                    <dd>{selectedBay.reserved_tier || 'unreserved'}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Confidence</dt>
                    <dd>{selectedBay.low_confidence ? 'Low (sensor offline)' : 'High'}</dd>
                  </div>
                </dl>
              ) : (
                <p className="text-xs text-slate-500">Select a bay on the map to inspect it.</p>
              )}
            </div>
          </section>
        </>
      )}
    </div>
  )
}
