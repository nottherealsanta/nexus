// The kernels and background shells of the viewed session tree.
import { Scroll, Text, View, effect, keymap, signal } from "nsh";
import { Button } from "nsh/base";
import { Heading, Modal, modalBodyHeight, ModalFooter, ModalTitle } from "./modal";
import { FOCUS, T } from "./kit";
import { loadResources, model, resourcesError, resourcesOpen, resourcesKind, toast, viewResources } from "../store";
import { request } from "../core/transport";

/** Stop (kill) a background shell; Core announces it to the session as a finished job. */
export async function stopShell(id: string): Promise<void> {
  try {
    await request("DELETE", `/api/bash/${encodeURIComponent(id)}`);
    toast("success", "Shell stopped");
  } catch (e) {
    toast("error", `Could not stop the shell: ${e instanceof Error ? e.message : String(e)}`);
  }
  void loadResources();
}
import { fmtDuration } from "./timeline";

const sessionLabel = (id?: string) => {
  if (!id) return "unknown session";
  return model.sessions.get(id)?.title?.trim() || id;
};

const statusClass = (status: string) =>
  status === "running" ? "text-info"
    : status === "done" ? "text-success"
    : status === "error" ? "text-error"
    : status === "timeout" || status === "killed" ? "text-warning"
    : "text-muted-foreground";

export function ResourcesModal() {
  return <If when={resourcesOpen()}>{() => <ResourcesBody />}</If>;
}

function ResourcesBody() {
  const r = () => viewResources();
  const running = () => r().jobs.filter((job) => job.status === "running").length;
  const title = () => resourcesKind() === "kernels" ? "Kernels" : resourcesKind() === "shells" ? "Shells" : "Kernels and shells";
  const close = () => resourcesOpen.set(false);
  // With shells listed, up/down select one and `s` stops it (the TUI's keys).
  const selected = signal(0);
  const jobs = () => resourcesKind() === "kernels" ? [] : r().jobs;
  const clamp = (i: number) => Math.max(0, Math.min(i, jobs().length - 1));
  const current = () => jobs()[clamp(selected())];
  const stopSelected = () => { const job = current(); if (job?.status === "running") void stopShell(job.id); };
  effect(() => {
    if (jobs().length === 0) return;
    const offs = [
      keymap.bind("down", () => selected.set(clamp(selected() + 1)), { when: "always" }),
      keymap.bind("up", () => selected.set(clamp(selected() - 1)), { when: "always" }),
      keymap.bind("s", stopSelected, { when: "notEditing" }),
    ];
    return () => offs.forEach((off) => off());
  });
  return (
    <Modal size="large" label={title()} testID="resources" onCancel={close} onOpenChange={(open) => { if (!open) close(); }}>
      <View class="gap-3">
        <ModalTitle title={title()} />
        <If when={!!resourcesError()}>
          <Text class="text-xs leading-4 text-warning">Could not refresh: {resourcesError()}</Text>
        </If>
        <Scroll style={{ height: Math.min(480, modalBodyHeight()) }} testID="resources-list">
          <View class="gap-2">
            <If when={resourcesKind() !== "shells"}><Heading text={`Kernels (${r().kernels.length})`} first />
            <If when={r().kernels.length === 0} else={
              <View class="gap-1">
                <Each of={r().kernels} key={(kernel) => kernel.id}>
                  {(kernel) => <KernelRow kernel={kernel()} />}
                </Each>
              </View>
            }>
              <Text class={T.sec}>No live kernels.</Text>
            </If></If>

            <If when={resourcesKind() !== "kernels"}><Heading text={`Background shells (${running()} live, ${r().jobs.length} kept)`} />
            <If when={r().jobs.length === 0} else={
              <View class="gap-1">
                <Each of={r().jobs} key={(job) => job.id}>
                  {(job, i) => <JobRow job={job()} selected={clamp(selected()) === i()} onSelect={() => selected.set(i())} />}
                </Each>
              </View>
            }>
              <Text class={T.sec}>No background shells.</Text>
            </If></If>
          </View>
        </Scroll>
        <ModalFooter hints={jobs().length > 0 ? ["↑↓ select", "s stop", "esc close"] : "esc close"} />
      </View>
    </Modal>
  );
}

function KernelRow(p: { kernel: any }) {
  const k = () => p.kernel;
  const variables = () => Array.isArray(k().variables) ? k().variables : [];
  const users = () => Array.isArray(k().users) ? k().users.length : 0;
  const age = () => Date.now() / 1000 - (typeof k().created === "number" ? k().created : Date.now() / 1000);
  return (
    <View testID={`resource-kernel-${k().id}`} class="rounded-(--radius) bg-card px-3 py-2 gap-1">
      <View class="flex-row items-center gap-2 min-w-0">
        <Text style={{ numberOfLines: 1 }} class="font-mono text-[12.5px] leading-5 text-foreground">{k().id}</Text>
        <Text class={`text-xs leading-4 ${k().busy ? "text-warning" : "text-muted-foreground"}`}>{k().busy ? "busy" : "idle"}</Text>
        <Text class={T.meta}>{`${k().cell_count ?? 0} ${k().cell_count === 1 ? "cell" : "cells"}`}</Text>
      </View>
      <Text style={{ numberOfLines: 1 }} class={T.meta}>{`created by ${sessionLabel(k().created_by)} · ${users()} ${users() === 1 ? "user" : "users"} · ${fmtDuration(age())} ago`}</Text>
      <If when={variables().length > 0}>
        <Text style={{ numberOfLines: 2 }} class="font-mono text-[12px] leading-4 text-foreground-secondary">
          {variables().map((v: any) => `${v.name}: ${v.type} = ${v.repr}`).join(" · ")}
        </Text>
      </If>
    </View>
  );
}

function JobRow(p: { job: any; selected: boolean; onSelect: () => void }) {
  const j = () => p.job;
  const chars = () => (typeof j().output_chars === "number" ? j().output_chars : 0);
  const dropped = () => (typeof j().dropped_chars === "number" ? j().dropped_chars : 0);
  return (
    <Button onPress={p.onSelect} selected={p.selected} accessibilityLabel={`Shell ${j().id}, ${j().status}`} testID={`resource-job-${j().id}`}
      class={`rounded-(--radius) px-3 py-2 gap-1 items-stretch ${p.selected ? "bg-selection" : "bg-card hover:bg-accent"} ${FOCUS}`}>
      <View class="flex-row items-center gap-2 min-w-0">
        <Text style={{ numberOfLines: 1 }} class="font-mono text-[12.5px] leading-5 text-foreground">{j().id}</Text>
        <Text class={`text-xs leading-4 ${statusClass(String(j().status))}`}>{j().status}</Text>
        <If when={typeof j().exit_code === "number"}><Text class={T.meta}>{`exit ${j().exit_code}`}</Text></If>
        <Text class={T.meta}>{fmtDuration(typeof j().duration_s === "number" ? j().duration_s : 0)}</Text>
        <View class="flex-1" />
        <If when={j().status === "running"}>
          <Button onPress={() => void stopShell(j().id)} accessibilityLabel={`Stop shell ${j().id}`} testID={`resource-job-stop-${j().id}`}
            class={`rounded-(--radius) px-2 min-h-6 justify-center bg-secondary hover:bg-accent ${FOCUS}`}>
            <Text class="text-xs leading-4 text-warning">Stop</Text>
          </Button>
        </If>
      </View>
      <Text style={{ numberOfLines: 1 }} class="font-mono text-[12px] leading-4 text-foreground-secondary">{`$ ${j().command ?? ""}`}</Text>
      <Text style={{ numberOfLines: 1 }} class={T.meta}>
        {`${sessionLabel(j().session_id)} · ${chars()} ${chars() === 1 ? "char" : "chars"}${dropped() > 0 ? ` · ${dropped()} dropped` : ""}${typeof j().timeout === "number" ? ` · timeout ${fmtDuration(j().timeout)}` : ""}`}
      </Text>
    </Button>
  );
}
