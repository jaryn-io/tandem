// biquote feed client. Primary transport is the SignalR tick hub (pushed
// ticks, no rate-limit cost); when the hub cannot be established the client
// falls back to batched REST polling of /api/latest, exactly as the feed
// documentation prescribes. The client never fabricates ticks: if neither
// transport delivers, it reports the connection as down and stays silent.

import { CONFIG } from "../config.js";

export class BiquoteFeed {
  // callbacks: onTick(rawTick), onConnection(state, detail?)
  constructor({ symbols, callbacks, config = CONFIG }) {
    this.symbols = symbols;
    this.callbacks = callbacks;
    this.config = config;
    this._connection = null;
    this._pollTimer = null;
    this._pollDown = false; // a poll failed since the last successful one
    this._started = false;
  }

  _state(state, detail) {
    this.callbacks.onConnection?.(state, detail);
  }

  async start() {
    if (this._started) return;
    this._started = true;
    this._state("connecting");
    try {
      await this._startSignalR();
      return;
    } catch (err) {
      this._state("reconnecting", `signalr unavailable: ${err?.message ?? err}`);
    }
    this._startPolling();
  }

  async stop() {
    this._started = false;
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
    if (this._connection) {
      try { await this._connection.stop(); } catch { /* already down */ }
      this._connection = null;
    }
    this._state("disconnected");
  }

  async _startSignalR() {
    if (typeof globalThis.signalR === "undefined") {
      throw new Error("signalR client library not loaded");
    }
    const connection = new globalThis.signalR.HubConnectionBuilder()
      // The hub's CORS policy is open-origin without credentials; the signalR
      // client defaults to sending credentials, which the browser rejects.
      .withUrl(this.config.hubUrl, { withCredentials: false })
      .withAutomaticReconnect()
      .build();

    connection.on("ReceiveTick", (tick) => this.callbacks.onTick?.(tick));
    connection.onreconnecting(() => this._state("reconnecting"));
    connection.onreconnected(async () => {
      this._state("streaming");
      await connection.invoke("Subscribe", this.symbols);
    });
    connection.onclose(() => {
      this._state("disconnected");
      if (this._started) this._startPolling();
    });

    await connection.start();
    await connection.invoke("Subscribe", this.symbols);
    this._connection = connection;
    this._state("streaming");
  }

  _startPolling() {
    if (this._pollTimer) return;
    this._pollDown = false;
    this._state("polling");
    this._pollTimer = setInterval(() => { void this._pollOnce(); }, this.config.pollIntervalMs);
    void this._pollOnce();
  }

  // One batched /api/latest poll. A failure marks the feed down; the next
  // success restores the "polling" state, so one network blip does not leave
  // the terminal "disconnected" — and fills suspended — while fresh quotes
  // are in fact arriving.
  async _pollOnce() {
    try {
      const qs = this.symbols.map((s) => `symbols=${encodeURIComponent(s)}`).join("&");
      const res = await fetch(`${this.config.apiBase}/api/latest?${qs}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      for (const s of this.symbols) {
        if (body[s]) this.callbacks.onTick?.(body[s]);
      }
      if (this._pollDown) {
        this._pollDown = false;
        this._state("polling");
      }
    } catch (err) {
      // Keep the last display untouched; just mark the feed unavailable.
      this._pollDown = true;
      this._state("disconnected", `poll failed: ${err?.message ?? err}`);
    }
  }

  // One-shot REST snapshot, used at boot so the first render shows the last
  // known prices immediately (including marketState when closed).
  async snapshot() {
    const qs = this.symbols.map((s) => `symbols=${encodeURIComponent(s)}`).join("&");
    const res = await fetch(`${this.config.apiBase}/api/latest?${qs}`);
    if (!res.ok) throw new Error(`snapshot failed: HTTP ${res.status}`);
    const body = await res.json();
    for (const s of this.symbols) {
      if (body[s]) this.callbacks.onTick?.(body[s]);
    }
    return body;
  }
}
