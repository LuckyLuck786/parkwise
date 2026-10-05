/** Mirrors backend/app/schemas/core.py. */

export type BayState = 'free' | 'allotted' | 'occupied' | 'blocked' | 'unknown'
export type VehicleType = 'two_wheeler' | 'four_wheeler' | 'any'

export interface Bay {
  id: string
  lot_id: string
  label: string
  bay_type: VehicleType
  is_accessible: boolean
  reserved_tier: number
  x: number
  y: number
  width: number
  height: number
  nearest_building_id: string | null
  state: BayState
  device_id: string | null
  sensor_fresh: boolean
  occupied_since: string | null
  low_confidence: boolean
}

export interface LotSummary {
  id: string
  name: string
  capacity: number
  free_bays: number
  occupied_bays: number
  allotted_bays: number
  blocked_bays: number
  low_confidence_bays: number
  accessible_bays: number
  fill_ratio: number
  x: number
  y: number
}

export interface LotOut {
  lot: LotSummary
  bays: Bay[]
}

export interface Building {
  id: string
  name: string
  x: number
  y: number
  has_step_free_entrance: boolean
  notes: string | null
}

export interface LotsResponse {
  lots: LotOut[]
  buildings: Building[]
  totals: Record<string, number>
  generated_at: string
  simulated: boolean
  note: string
}

export interface HealthResponse {
  status: string
  app: string
  version: string
  environment: string
  database: string
}
