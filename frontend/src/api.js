async function req(path, opts = {}) {
  const r = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  const text = await r.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!r.ok) {
    const detail = (data && data.detail) || text || r.statusText
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return data
}

export const api = {
  dashboard: () => req('/dashboard'),
  campaigns: () => req('/campaigns'),
  events: (day) => req('/events' + (day ? `?business_day=${encodeURIComponent(day)}` : '')),
  addEvent: (body) => req('/events', { method: 'POST', body: JSON.stringify(body) }),
  // 核销写入口二（API 专用），与页面 /events 的 redeem 共用后端同一业务日写闸
  redeem: (body) => req('/redeem', { method: 'POST', body: JSON.stringify(body) }),
  facts: (day) => req('/facts' + (day ? `?business_day=${encodeURIComponent(day)}` : '')),
  preview: (day) => req('/redeem/preview' + (day ? `?business_day=${encodeURIComponent(day)}` : '')),
  seal: (body) => req('/redeem/seal', { method: 'POST', body: JSON.stringify(body) }),
  batches: () => req('/batches'),
  batchDetail: (id) => req(`/batches/${id}`),
  orphans: (day) => req('/redeem/orphans' + (day ? `?business_day=${encodeURIComponent(day)}` : '')),
  quarantine: (body) => req('/redeem/quarantine', { method: 'POST', body: JSON.stringify(body || {}) }),
  getDay: () => req('/settings/day'),
  setDay: (business_day) => req('/settings/day', { method: 'PUT', body: JSON.stringify({ business_day }) }),
}
