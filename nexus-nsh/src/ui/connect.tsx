import { Scroll, Text, View, effect, keyboardFrame, platform, safeArea, signal } from "nsh";
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
  return (
    <Scroll class="flex-1 bg-background" testID="connect"
      contentClass="grow justify-center px-6"
      contentStyle={{ paddingTop: safeArea().top + 24, paddingBottom: safeArea().bottom + 24 + keyboardFrame().height }}>
      <View class="w-full max-w-[420px] gap-4">
        <View class="gap-1.5">
          <Text class="text-[20px] leading-7 font-semibold text-foreground">Connect to Nexus Core</Text>
          <Text class={`${T.sec} text-muted-foreground`}>
            This app talks to Core directly over your network. Enter the address Core serves on — for example
            http://192.168.1.20:4747 — with Core started as `NEXUS_HOST=0.0.0.0 nexus serve`.
          </Text>
        </View>
        <View class="gap-1.5">
          <Text class={T.meta}>Core address</Text>
          <Input
            value={address()} testID="connect-address" accessibilityLabel="Core address"
            placeholder="http://192.168.1.20:4747" autoCapitalize="none" autoCorrect={false}
            submitBehavior="submit" onSubmit={() => void submit()}
            onChange={({ value }) => { error.set(null); address.set(value); }}
            class={`h-11 rounded-(--radius) border bg-card px-3 text-[15px] text-foreground placeholder:text-muted-foreground ${FOCUS} ${error() ? "border-error" : "border-border"}`}
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
  );
}
