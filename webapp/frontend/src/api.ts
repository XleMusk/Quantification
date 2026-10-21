export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { ...options, headers: { 'X-Local-Request': '1', ...options?.headers } });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = typeof payload.detail === 'string' ? payload.detail : Array.isArray(payload.detail) ? payload.detail.map((v: { msg: string }) => v.msg).join('; ') : `请求失败 (${response.status})`;
    throw new Error(detail);
  }
  return response.json();
}
export function post<T>(path: string, body?: unknown, key?: string): Promise<T> {
  return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(key ? { 'Idempotency-Key': key } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
}
