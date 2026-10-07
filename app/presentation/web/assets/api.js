export class APIError extends Error {
  constructor(status, detail, code) {
    super(detail);
    this.status = status;
    this.code = code;
  }
}

export class API {
  constructor(prefix, token = "", signal) {
    this.prefix = prefix;
    this.token = token;
    this.signal = signal;
  }

  async request(path, options = {}) {
    const headers = new Headers(options.headers);
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    const signals = [this.signal, options.signal].filter(Boolean);
    const response = await fetch(this.prefix + path, {
      ...options, headers, cache: "no-store", credentials: "omit",
      signal: signals.length ? AbortSignal.any(signals) : undefined,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new APIError(response.status, typeof body.detail === "string" ? body.detail : `Request failed (${response.status}).`, body.code);
    }
    return response;
  }

  async json(path, options) {
    const response = await this.request(path, options);
    const text = await response.text();
    this.signal?.throwIfAborted();
    options?.signal?.throwIfAborted();
    return text ? JSON.parse(text) : null;
  }
}

export function queryString(values) {
  return new URLSearchParams(Object.entries(values).filter(([, value]) => value !== "" && value != null)).toString();
}

export function identifier(value) {
  const text = value.trim();
  if (!/^[a-f\d]{8}(-[a-f\d]{4}){3}-[a-f\d]{12}$/i.test(text)) throw new Error("Enter a valid UUID.");
  return text;
}
