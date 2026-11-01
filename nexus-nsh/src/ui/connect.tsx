import { Scroll, Text, View, effect, keyboardFrame, platform, safeArea, signal, windowSize } from "nsh";
import { Input } from "nsh/base";
import { Btn, FOCUS, T } from "./kit";
import { connectCore, coreUrl } from "../store";

/** Native only: the Connect screen is open over a running app to change the saved address. */
export const coreEditing = signal(false);
const address = signal("");
const error = signal<string | null>(null);
const busy = signal(false);

/** The last opened state, so the field starts from the saved address without clobbering typing. */
let filled = "";

const noise = (a: number, b: number) => { const s = Math.sin(a * 127.1 + b * 311.7) * 43758.5453; return s - Math.floor(s); };
/** Rows of the logo's `_/` mark in soft clusters, clear around the middle where the form sits. */
export function slashField(cols: number, rows: number): string[] {
  const blobs = [[0.14, 0.22, 0.34], [0.86, 0.18, 0.32], [0.16, 0.8, 0.3], [0.86, 0.76, 0.36], [0.5, 0.06, 0.2], [0.55, 0.96, 0.22]];
  const out: string[] = [];
  for (let r = 0; r < rows; r++) {
    let line = "";
    for (let c = 0; c < cols; c++) {
      const x = c / Math.max(1, cols - 1), y = r / Math.max(1, rows - 1);
      const inForm = Math.abs(x - 0.5) < 0.3 && Math.abs(y - 0.5) < 0.3;
      let d = 0;
      for (const [bx, by, br] of blobs) d = Math.max(d, 1 - Math.hypot(x - bx, y - by) / br);
      line += !inForm && d > 0 && noise(c, r) < d * 1.15 ? "_/ " : "   ";
    }
    out.push(line);
  }
  return out;
}

/**
 * Connect to Core by address (DESIGN_PLAN / native targets): the iOS and macOS builds are on
 * the same network as Core and need no pairing, token or host process — just where it listens.
 */
export function ConnectScreen() {
  effect(() => {
    const saved = coreUrl();
    const key = `${coreEditing()}|${saved ?? ""}`;
    if (key === filled) return;
    filled = key;
    // A Mac's Core is usually on this machine; a phone's is on the network, so it types the address.
    address.set(saved ?? (platform.os === "macos" ? "http://127.0.0.1:4747" : ""));
    error.set(null);
  });
  const submit = async () => {
    if (busy()) return;
    error.set(null);
    busy.set(true);
    try {
      await connectCore(address());
      coreEditing.set(false);
    } catch (e) {
      error.set(e instanceof Error ? e.message : String(e));
    } finally {
      busy.set(false);
    }
  };
  const field = () => slashField(Math.ceil(windowSize().width / 20), Math.ceil(windowSize().height / 16));
  return (
    <View class="flex-1 bg-background">
    <View class="absolute inset-0 overflow-hidden" style={{ pointerEvents: "none" }} testID="connect-field" accessible={false}>
      <Each of={field()}>{(row) => <Text style={{ numberOfLines: 1 }} class="font-mono text-[11px] leading-4 text-border-strong">{row()}</Text>}</Each>
    </View>
    <Scroll class="flex-1" testID="connect"
      contentClass="grow justify-center px-6"
      contentStyle={{ paddingTop: safeArea().top + 24, paddingBottom: safeArea().bottom + 24 + keyboardFrame().height }}>
      <View class="w-full max-w-[420px] gap-4">
        <View class="gap-1.5">
          <Text class="text-[20px] leading-7 font-semibold text-foreground">Connect to Core</Text>
          <If when={platform.os === "macos"} else={
            <Text class={`${T.sec} text-muted-foreground`}>
              This app talks to Core directly over your network. Enter the address Core serves on — for example
              http://192.168.1.20:4747 — with Core started as `NEXUS_HOST=0.0.0.0 nexus serve`.
            </Text>
          }>
            <Text class={`${T.sec} text-muted-foreground`}>
              A local Core is found automatically from `~/.nexus/daemon.json`, token included. To use a Core on
              another machine, enter its address — for example http://192.168.1.20:4747 — started with
              `NEXUS_HOST=0.0.0.0 nexus serve`.
            </Text>
          </If>
        </View>
        <View class="gap-1.5">
          <Text class={T.meta}>Core address</Text>
          <Input
            value={address()} testID="connect-address" accessibilityLabel="Core address"
            placeholder="http://192.168.1.20:4747" autoCapitalize="none" autoCorrect={false}
            submitBehavior="submit" onSubmit={() => void submit()}
            onChange={({ value }) => { error.set(null); address.set(value); }}
            class={`h-11 rounded-(--radius) px-3 text-[15px] text-foreground placeholder:text-muted-foreground ${FOCUS} ${error() ? "bg-error/10" : "bg-card"}`}
          />
          <If when={!!error()}>
            <Text testID="connect-error" class="text-[13px] leading-5 text-error">{error()!}</Text>
          </If>
        </View>
        <View class="flex-row items-center gap-2">
          <Btn label="Connect" kind="primary" busy={busy()} onPress={() => void submit()} testID="connect-submit" />
          <If when={!!coreUrl()}>
            <Btn label="Cancel" kind="ghost" onPress={() => coreEditing.set(false)} testID="connect-cancel" />
          </If>
          <View class="flex-1" />
          <Text class={T.meta}>No token or pairing code</Text>
        </View>
      </View>
    </Scroll>
    </View>
  );
}
