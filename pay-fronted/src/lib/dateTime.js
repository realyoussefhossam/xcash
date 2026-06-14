const TIMEZONE_PATTERN = /(Z|[+-]\d{2}:?\d{2})$/i

export function parseServerDateMs(value) {
  if (!value) return null
  if (typeof value !== "string") {
    const timestamp = new Date(value).getTime()
    return Number.isNaN(timestamp) ? null : timestamp
  }

  const normalized = TIMEZONE_PATTERN.test(value) ? value : `${value}Z`
  const timestamp = new Date(normalized).getTime()
  return Number.isNaN(timestamp) ? null : timestamp
}

export function getRemainingMs(expiresAt) {
  const expireTimestamp = parseServerDateMs(expiresAt)
  if (expireTimestamp === null) return null
  return Math.max(0, expireTimestamp - Date.now())
}
