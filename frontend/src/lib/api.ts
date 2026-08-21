const API_BASE_URL = String(import.meta.env.VITE_API_BASE_URL || "")
  .trim()
  .replace(/\/+$/, "")

/** Resolve an API-owned path while preserving the existing same-origin setup. */
export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path
  const normalized = path.startsWith("/") ? path : `/${path}`
  return API_BASE_URL ? `${API_BASE_URL}${normalized}` : normalized
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`)
  return payload as T
}
