// Request-scoped streaming checkpoints, independent of durable event cursors.
export class LivePartial {
  requestId = "";
  seq = 0;
  parts: any[] = [];
  seeded = false;
  pending: any[] = [];

  event(d: any) {
    if (this.requestId !== d.request_id) {
      this.requestId = d.request_id; this.seq = 0; this.parts = []; this.seeded = false; this.pending = [];
    }
    if (d.seq <= this.seq) return;
    if (this.seeded && d.seq > this.seq + 1) this.seeded = false;
    if (!this.seeded && d.seq === 1) this.seeded = true;
    if (!this.seeded) { this.pending.push(d); return; }
    this.fold(d);
  }
  seed(c: any) {
    if (!c?.request_id || (this.requestId && this.requestId !== c.request_id)) return;
    if (this.seeded && this.seq >= c.seq) return;
    this.requestId = c.request_id; this.seq = c.seq; this.parts = JSON.parse(JSON.stringify(c.parts)); this.seeded = true;
    const pending = this.pending; this.pending = [];
    for (const d of pending.sort((a, b) => a.seq - b.seq)) if (d.seq > this.seq) this.fold(d);
  }
  private fold(d: any) {
    const i = d.part_index;
    if (!Number.isInteger(i) || i < 0 || i > 100000) return;
    if (d.type === "part_done") this.parts[i] = d.part;
    else if (d.type === "text_delta" || d.type === "reasoning_delta") {
      const field = d.type === "text_delta" ? "text" : "summary";
      this.parts[i] ??= { type: d.type === "text_delta" ? "text" : "reasoning", [field]: "" };
      this.parts[i][field] = (this.parts[i][field] ?? "") + d.text;
    }
    this.seq = d.seq;
  }
  text() {
    return {
      text: this.parts.filter((p) => p?.type === "text").map((p) => p.text ?? "").join(""),
      reasoning: this.parts.filter((p) => p?.type === "reasoning").map((p) => p.summary ?? "").join(""),
    };
  }
}
