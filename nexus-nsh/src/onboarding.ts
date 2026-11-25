// First-run onboarding (plans/52). The steps, agent rows and wording are in interface/manifest.toml ([onboarding]); the
// TUI draws the same tour (nexus-tui/src/onboarding.rs). This file is the state and the rules, with no UI in it.
import { signal } from "nsh";
import { manifest } from "./gen/manifest";
import { searchOnboardingAsked } from "./search-service";

export interface OnboardingStep { id: string; kind: "info" | "provider" | "agents" | "search" | "done"; title: string; lines: readonly string[]; tip?: string }
export interface OnboardingAgent { name: string; kind: "model" | "root"; role: string; hint: string; options?: readonly string[] }

export const ONBOARDING = manifest.onboarding as unknown as {
  finish_no_project: string; no_provider: string; quest_provider: string; hints_info: string; hints_rows: string; hints_search: string; search_retry: string;
  step: readonly OnboardingStep[]; agent: readonly OnboardingAgent[];
};
export const onboardingSteps = ONBOARDING.step;

/** The tour is showing (it covers the whole window). */
export const onboardingOpen = signal(false);
export const onboardingStep = signal(0);
/** The tour was offered or closed in this run, so a config reload never opens it again. */
export const onboardingOffered = signal(false);

/** Open when Core says this home has not finished or skipped it: `ui.onboarded` is exactly false (an older Core has no key). */
export function shouldOfferOnboarding(config: any, phone: boolean, offered: boolean): boolean {
  return !phone && !offered && config?.values?.ui?.onboarded === false;
}

export function openOnboarding() {
  onboardingOffered.set(true);
  onboardingStep.set(0);
  searchOnboardingAsked.set(false);
  onboardingOpen.set(true);
}
export function closeOnboarding() { onboardingOpen.set(false); }
export function nextStep() { onboardingStep.update((i) => Math.min(onboardingSteps.length - 1, i + 1)); }
export function previousStep() { onboardingStep.update((i) => Math.max(0, i - 1)); }
export const currentStep = () => onboardingSteps[Math.min(onboardingStep(), onboardingSteps.length - 1)];
export const lastStep = () => onboardingStep() >= onboardingSteps.length - 1;

/** `{key:command}` becomes that command's key as the client writes it; a command with no key reads as its slash name. */
export function fillKeys(text: string, label: (command: string) => string | null): string {
  return text.replace(/\{key:([a-z0-9_.]+)\}/g, (_, id: string) => label(id) ?? `/${(manifest.command as readonly any[]).find((c) => c.id === id)?.slash ?? id}`);
}

/** What an agent row shows: the models set for it, or what it uses without any. */
export function agentModelLine(config: any, name: string): string {
  const refs: string[] = config?.values?.agents?.models?.[name] ?? [];
  if (!refs.length) return "the session's model";
  return refs.length === 1 ? refs[0] : `${refs[0]} +${refs.length - 1}`;
}

/** The config patch that gives one agent exactly this one model (or none), leaving the other agents as they are. */
export function agentModelsPatch(config: any, name: string, ref: string | null): Record<string, string[]> {
  const all: Record<string, string[]> = { ...(config?.values?.agents?.models ?? {}) };
  if (ref) all[name] = [ref]; else delete all[name];
  return all;
}

/** The root agent a new session starts with: the setting, else the first option (`build`, Core's own default). */
export function rootAgent(config: any, options: readonly string[]): string {
  const set = config?.values?.defaults?.agent;
  return options.includes(set) ? set : options[0];
}
