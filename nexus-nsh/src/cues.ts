// Notification cues (Settings › Notifications › Notification sounds).
// Best effort: a browser without Web Audio, or one blocking playback, stays silent.
type ParamLike = {
  value: number;
  setValueAtTime(value: number, when: number): void;
  exponentialRampToValueAtTime(value: number, when: number): void;
};
type NodeLike = { connect(node: unknown): NodeLike };
type OscillatorLike = { type: string; frequency: ParamLike; connect(node: unknown): NodeLike; start(when: number): void; stop(when: number): void };
type GainLike = { gain: ParamLike; connect(node: unknown): NodeLike };
type AudioContextLike = {
  currentTime: number;
  destination: unknown;
  createOscillator(): OscillatorLike;
  createGain(): GainLike;
  resume?(): Promise<void>;
  close(): Promise<void>;
};
type AudioContextConstructor = new () => AudioContextLike;

/** Finished is the TUI's rising two-note cue; input is higher and distinct. */
const CUES = { finished: [660, 1046.5], input: [880, 1174.7] } as const;
export type NotificationCue = keyof typeof CUES;

export function playNotificationCue(kind: NotificationCue) {
  try {
    const root = globalThis as unknown as { AudioContext?: AudioContextConstructor; webkitAudioContext?: AudioContextConstructor };
    const Context = root.AudioContext ?? root.webkitAudioContext;
    if (!Context) return;
    const context = new Context();
    void context.resume?.().catch(() => {});
    const now = context.currentTime;
    CUES[kind].forEach((frequency, index) => {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      const start = now + index * 0.1;
      oscillator.frequency.value = frequency;
      oscillator.type = "sine";
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.1, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.09);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start(start);
      oscillator.stop(start + 0.1);
    });
    setTimeout(() => void context.close().catch(() => {}), CUES[kind].length * 100 + 100);
  } catch { /* Audio cues are optional. */ }
}
