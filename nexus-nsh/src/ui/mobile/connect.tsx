// The phone's Connect screen (plans/05 section 5.2; absorbs plans/09 9.6's Connect item): shown on first run and when
// the saved Core cannot be reached. A numbered setup, a pairing link (what `nexus pair` prints), a manual address,
// recent Cores, and a clear Local Network error with Retry.
import { Clipboard, Scroll, Storage, Text, View, effect, keyboardFrame, safeArea, signal } from "nsh";
import { Button, Input } from "nsh/base";
import { Btn, FOCUS, Icon, T, TS } from "../kit";
import { connectProblem, parsePairing, rememberCore, transportOf, hostLabel, type RecentCore } from "../../mobile";
import { connectCore, coreUrl } from "../../store";

const RECENT_KEY = "nexus.recent-cores";
const recent = signal<RecentCore[]>([]);
const link = signal("");
const manual = signal("");
const manualOpen = signal(false);
const busy = signal(false);
const problem = signal<{ title: string; body: string; url: string } | null>(null);
let loaded = false;
let last = "";

async function loadRecent() {
  if (loaded) return;
  loaded = true;
  try { const raw = await Storage.getItem(RECENT_KEY); if (raw) recent.set(JSON.parse(raw)); } catch { /* none saved */ }
}
function saveRecent(list: RecentCore[]) { recent.set(list); try { void Storage.setItem(RECENT_KEY, JSON.stringify(list)); } catch { /* storage unavailable */ } }

function Step(p: { n: number; title: string; children?: any }) {
  return (
    <View class="flex-row gap-3" testID={`connect-step-${p.n}`}>
      <View class="size-7 items-center justify-center rounded-full bg-primary" accessible={false}><Text class="font-(family-name:--font-ui) text-[14px] leading-5 font-semibold text-on-accent">{String(p.n)}</Text></View>
      <View class="flex-1 min-w-0 gap-1 pt-0.5">
        <Text class={`${TS.body} font-medium text-foreground`}>{p.title}</Text>
        {p.children}
      </View>
    </View>
  );
}

export function PhoneConnect(p: { onDone: () => void; canCancel: boolean }) {
  effect(() => { void loadRecent(); });
  const go = async (raw: string, name?: string) => {
    if (busy()) return;
    const parsed = parsePairing(raw);
    if (!parsed) { problem.set({ title: "That is not an address or a pairing link", body: "Paste the nexus://pair link that `nexus pair` prints, or type host:port (for example 192.168.1.20:4747).", url: "" }); return; }
    last = raw;
    problem.set(null);
    busy.set(true);
    try {
      await connectCore(parsed.url);
      saveRecent(rememberCore(recent(), { url: parsed.url, name: name ?? parsed.name, at: Date.now() }));
      link.set(""); manual.set("");
      p.onDone();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      problem.set({ ...connectProblem(msg, parsed.url), url: parsed.url });
    } finally { busy.set(false); }
  };
  const paste = async () => {
    try { const t = await Clipboard.readText(); if (t) { link.set(t.trim()); void go(t.trim()); } } catch { /* the user denied the paste */ }
  };
  return (
    <View class="flex-1 bg-background" testID="phone-connect">
      <Scroll class="flex-1" testID="connect" contentStyle={{ paddingTop: safeArea().top + 20, paddingBottom: safeArea().bottom + 24 + keyboardFrame().height }}>
        <View class="px-5 gap-6">
          <View class="gap-2">
            <Text class="font-mono text-[28px] leading-9 font-bold text-primary" accessible={false}>_/</Text>
            <Text class="font-(family-name:--font-ui) text-[26px] leading-8 font-semibold text-foreground">Connect to Nexus</Text>
            <Text class={`${TS.body} text-muted-foreground`}>Nexus runs on your computer. This phone connects to it over your Wi-Fi or Tailscale.</Text>
          </View>

          <If when={!!problem()}>
            <View class="rounded-2xl bg-error/10 p-4 gap-2" role="alert" testID="connect-error">
              <Text class={`${TS.body} font-semibold text-error`} testID="connect-error-title">{problem()!.title}</Text>
              <Text class={`${TS.base} text-foreground-secondary`} testID="connect-error-body">{problem()!.body}</Text>
              <View class="flex-row gap-2 pt-1">
                <Btn label="Retry" kind="primary" busy={busy()} onPress={() => void go(last || problem()!.url)} testID="connect-retry" class="min-h-11" />
              </View>
            </View>
          </If>

          <View class="rounded-2xl bg-raised p-4 gap-4" testID="connect-steps">
            <Step n={1} title="Install and start Nexus on your computer">
              <Text class={`${T.mono} text-[13px] leading-5`}>nexus daemon start</Text>
            </Step>
            <Step n={2} title="Run the pairing command">
              <Text class={`${T.mono} text-[13px] leading-5`}>nexus pair</Text>
              <Text class={T.meta}>It prints the address and a QR code. No account or relay is involved.</Text>
            </Step>
            <Step n={3} title="Scan the QR code or paste the link">
              <Text class={T.meta}>Scan the code with the Camera or any QR reader, copy the link it shows, and paste it here.</Text>
            </Step>
          </View>

          <View class="gap-2">
            <Text class={`${T.section}`}>Pairing link</Text>
            <View class="flex-row items-center gap-2">
              <Input value={link()} onChange={({ value }) => { problem.set(null); link.set(value); }} onSubmit={() => void go(link())} submitBehavior="submit"
                placeholder="nexus://pair?url=…" autoCapitalize="none" autoCorrect={false} accessibilityLabel="Pairing link" testID="connect-link"
                class={`h-12 flex-1 min-w-0 rounded-xl bg-raised px-3 ${TS.body} text-foreground placeholder:text-muted-foreground ${FOCUS}`} />
              <Btn label="Paste" onPress={() => void paste()} testID="connect-paste" class="min-h-12" />
            </View>
            <Btn label="Connect" kind="primary" busy={busy()} disabled={!link().trim()} onPress={() => void go(link())} testID="connect-submit" class="min-h-12" />
          </View>

          <View class="gap-2">
            <Button onPress={() => manualOpen.update((v) => !v)} testID="connect-manual-toggle" accessibilityLabel="Enter a server manually"
              class={`flex-row items-center gap-2 min-h-11 ${FOCUS}`}>
              <Icon name={manualOpen() ? "chevron-down" : "chevron-right"} size={14} class="tint-muted-foreground" />
              <Text class={`${TS.body} text-foreground-secondary`}>Enter a server manually</Text>
            </Button>
            <If when={manualOpen()}>
              <View class="flex-row items-center gap-2">
                <Input value={manual()} onChange={({ value }) => { problem.set(null); manual.set(value); }} onSubmit={() => void go(manual())} submitBehavior="submit"
                  placeholder="192.168.1.20:4747" autoCapitalize="none" autoCorrect={false} accessibilityLabel="Core address" testID="connect-address"
                  class={`h-12 flex-1 min-w-0 rounded-xl bg-raised px-3 ${TS.body} text-foreground placeholder:text-muted-foreground ${FOCUS}`} />
                <Btn label="Connect" onPress={() => void go(manual())} disabled={!manual().trim()} testID="connect-address-submit" class="min-h-12" />
              </View>
            </If>
          </View>

          <If when={recent().length > 0}>
            <View class="gap-2" testID="connect-recent">
              <Text class={T.section}>Recent Cores</Text>
              <View class="rounded-2xl bg-raised overflow-hidden">
                <Each of={recent()} key={(c) => c.url}>
                  {(c) => (
                    <Button onPress={() => void go(c().url, c().name)} testID={`recent-${c().url}`} accessibilityLabel={`Connect to ${c().name || hostLabel(c().url)}`}
                      class={`flex-row items-center gap-3 px-4 min-h-14 active:bg-selection ${FOCUS}`}>
                      <View class="flex-1 min-w-0">
                        <Text style={{ numberOfLines: 1 }} class={`${TS.body} text-foreground`}>{c().name || hostLabel(c().url)}</Text>
                        <Text style={{ numberOfLines: 1 }} class={T.meta}>{c().url}</Text>
                      </View>
                      <View class="rounded-full bg-muted px-2.5 min-h-6 justify-center"><Text class="font-(family-name:--font-ui) text-[12px] leading-5 text-foreground-secondary">{transportOf(c().url)}</Text></View>
                    </Button>
                  )}
                </Each>
              </View>
            </View>
          </If>

          <If when={p.canCancel}><Btn label={coreUrl() ? "Back" : "Cancel"} kind="ghost" onPress={p.onDone} testID="connect-cancel" class="min-h-12" /></If>
        </View>
      </Scroll>
    </View>
  );
}
