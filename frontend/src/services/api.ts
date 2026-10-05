import axios from 'axios'

import type { HealthResponse, LotsResponse } from '@/types'

export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

export const api = axios.create({ baseURL: API_BASE_URL, timeout: 20_000 })

export function apiError(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail
    if (typeof detail === 'string') return detail
    return error.message
  }
  return error instanceof Error ? error.message : 'Something went wrong'
}

export const fetchHealth = () => api.get<HealthResponse>('/health').then((r) => r.data)

export const fetchLots = () => api.get<LotsResponse>('/lots').then((r) => r.data)
