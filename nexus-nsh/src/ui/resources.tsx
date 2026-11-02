// The kernels and background shells of the viewed session tree: the counts on
// the location strip open this one scrollable list.
import { Scroll, Text, View, windowSize } from "nsh";
import { Heading, Modal, ModalFooter, ModalTitle } from "./modal";
import { T } from "./kit";
import { model, resourcesError, resourcesOpen, viewResources } from "../store";
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
  const close = () => resourcesOpen.set(false);
  return (
    <Modal width={700} top="top-[10%]" label="Kernels and shells" testID="resources" onCancel={close} onOpenChange={(open) => { if (!open) close(); }}>
      <View class="gap-3">
        <ModalTitle title="Kernels and shells" esc={false} />
        <If when={!!resourcesError()}>
          <Text class="text-xs leading-4 text-warning">Could not refresh: {resourcesError()}</Text>
        </If>
        <Scroll style={{ height: Math.min(480, Math.round(windowSize().height * 0.55)) }} testID="resources-list">
          <View class="gap-2">
            <Heading text={`Kernels (${r().kernels.length})`} first />
            <If when={r().kernels.length === 0} else={
              <View class="gap-1">
                <Each of={r().kernels} key={(kernel) => kernel.id}>
                  {(kernel) => <KernelRow kernel={kernel()} />}
                </Each>
              </View>
            }>
              <Text class={T.sec}>No live kernels.</Text>
            </If>

            <Heading text={`Background shells (${running()} live, ${r().jobs.length} kept)`} />
            <If when={r().jobs.length === 0} else={
              <View class="gap-1">
                <Each of={r().jobs} key={(job) => job.id}>
                  {(job) => <JobRow job={job()} />}
                </Each>
              </View>
            }>
              <Text class={T.sec}>No background shells.</Text>
            </If>
          </View>
        </Scroll>
        <ModalFooter hints="esc close" />
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

function JobRow(p: { job: any }) {
  const j = () => p.job;
  const chars = () => (typeof j().output_chars === "number" ? j().output_chars : 0);
  const dropped = () => (typeof j().dropped_chars === "number" ? j().dropped_chars : 0);
  return (
    <View testID={`resource-job-${j().id}`} class="rounded-(--radius) bg-card px-3 py-2 gap-1">
      <View class="flex-row items-center gap-2 min-w-0">
        <Text style={{ numberOfLines: 1 }} class="font-mono text-[12.5px] leading-5 text-foreground">{j().id}</Text>
        <Text class={`text-xs leading-4 ${statusClass(String(j().status))}`}>{j().status}</Text>
        <If when={typeof j().exit_code === "number"}><Text class={T.meta}>{`exit ${j().exit_code}`}</Text></If>
        <Text class={T.meta}>{fmtDuration(typeof j().duration_s === "number" ? j().duration_s : 0)}</Text>
      </View>
      <Text style={{ numberOfLines: 1 }} class="font-mono text-[12px] leading-4 text-foreground-secondary">{`$ ${j().command ?? ""}`}</Text>
      <Text style={{ numberOfLines: 1 }} class={T.meta}>
        {`${sessionLabel(j().session_id)} · ${chars()} ${chars() === 1 ? "char" : "chars"}${dropped() > 0 ? ` · ${dropped()} dropped` : ""}${typeof j().timeout === "number" ? ` · timeout ${fmtDuration(j().timeout)}` : ""}`}
      </Text>
    </View>
  );
}
