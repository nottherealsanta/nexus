//! First-run onboarding (plans/52): a full-window tour that connects a provider, sets models for agents and teaches
//! the basics. NSH draws the same steps (`nexus-nsh/src/ui/onboarding.tsx`); both read them from
//! `interface/manifest.toml` (`[onboarding]`). This module is the data, the state and the rules; `render.rs` draws.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::app::{App, Effect};
use crate::manifest::Manifest;
use crate::state::ConnectionState;
use crate::sync::Command as SyncCommand;
use crate::ui::{Focus, Level, PaletteMode, View};

#[derive(Debug, Clone, Deserialize)]
pub struct Tour {
    pub finish_no_project: String,
    pub no_provider: String,
    pub quest_provider: String,
    pub hints_info: String,
    pub hints_rows: String,
    pub hints_search: String,
    pub search_retry: String,
    #[serde(default)]
    pub step: Vec<Step>,
    #[serde(default)]
    pub agent: Vec<AgentRow>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Step {
    pub id: String,
    pub kind: String,
    pub title: String,
    #[serde(default)]
    pub lines: Vec<String>,
    pub tip: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AgentRow {
    pub name: String,
    pub kind: String,
    pub role: String,
    pub hint: String,
    #[serde(default)]
    pub options: Vec<String>,
}

impl AgentRow {
    pub fn is_root(&self) -> bool {
        self.kind == "root"
    }
}

/// Where the user is in the tour.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Onboarding {
    pub step: usize,
    /// The selected row on a provider or agents step.
    pub row: usize,
}

impl Tour {
    pub fn of(manifest: &Manifest) -> Tour {
        manifest.onboarding.clone()
    }

    pub fn current(&self, state: &Onboarding) -> &Step {
        &self.step[state.step.min(self.step.len() - 1)]
    }

    pub fn is_last(&self, state: &Onboarding) -> bool {
        state.step + 1 >= self.step.len()
    }

    /// A step with selectable rows: providers or agents.
    pub fn has_rows(&self, state: &Onboarding) -> bool {
        matches!(self.current(state).kind.as_str(), "provider" | "agents")
    }

    pub fn is_search(&self, state: &Onboarding) -> bool {
        self.current(state).kind == "search"
    }

    pub fn hints(&self, state: &Onboarding) -> &str {
        if self.has_rows(state) {
            &self.hints_rows
        } else if self.is_search(state) {
            &self.hints_search
        } else {
            &self.hints_info
        }
    }
}

/// Core says this home has not finished or skipped the tour: `ui.onboarded` is exactly `false` (an older Core has no key).
pub fn should_offer(config: Option<&Value>) -> bool {
    config.is_some_and(|c| c["values"]["ui"]["onboarded"] == json!(false))
}

/// `{key:command}` becomes that command's TUI key; a command with no key reads as its slash name.
pub fn fill_keys(manifest: &Manifest, text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(start) = rest.find("{key:") {
        out.push_str(&rest[..start]);
        let after = &rest[start + 5..];
        let Some(end) = after.find('}') else {
            out.push_str(&rest[start..]);
            return out;
        };
        let id = &after[..end];
        let command = manifest.command(id);
        let label = command
            .and_then(|c| c.tui_key().map(key_label))
            .or_else(|| command.and_then(|c| c.slash.as_ref().map(|s| format!("/{s}"))))
            .unwrap_or_else(|| id.to_owned());
        out.push_str(&label);
        rest = &after[end + 1..];
    }
    out.push_str(rest);
    out
}

/// A manifest key as the TUI writes it in hints: `ctrl+p` stays, `esc` and `enter` stay.
fn key_label(key: &str) -> String {
    key.to_owned()
}

/// What an agent row shows: the model set for it, or what it uses without one.
pub fn agent_model_line(config: Option<&Value>, name: &str) -> String {
    let refs: Vec<&str> = config
        .and_then(|c| c["values"]["agents"]["models"][name].as_array())
        .map(|l| l.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    match refs.as_slice() {
        [] => "the session's model".into(),
        [one] => (*one).to_owned(),
        [first, more @ ..] => format!("{first} +{}", more.len()),
    }
}

/// `[agents.models]` with exactly this one model for `name` (or none), every other agent as it was.
pub fn agent_models_patch(config: Option<&Value>, name: &str, model: Option<&str>) -> Value {
    let mut all = config
        .and_then(|c| c["values"]["agents"]["models"].as_object().cloned())
        .unwrap_or_default();
    match model {
        Some(model) => {
            all.insert(name.to_owned(), json!([model]));
        }
        None => {
            all.remove(name);
        }
    }
    Value::Object(all)
}

/// The root agent a new session starts with: `defaults.agent` when it is one of the options, else the first.
pub fn root_agent(config: Option<&Value>, options: &[String]) -> String {
    let set = config.and_then(|c| c["values"]["defaults"]["agent"].as_str());
    set.filter(|s| options.iter().any(|o| o == s))
        .or_else(|| options.first().map(String::as_str))
        .unwrap_or_default()
        .to_owned()
}

/// The option after the current root agent, wrapping.
pub fn next_root_agent(config: Option<&Value>, options: &[String]) -> String {
    let now = root_agent(config, options);
    let at = options.iter().position(|o| *o == now).unwrap_or(0);
    options[(at + 1) % options.len()].clone()
}

/// What a row on the provider or agents step lists, in order (providers hide the mock one).
impl App {
    pub fn onboarding_providers(&self) -> Vec<&Value> {
        self.state
            .providers
            .iter()
            .filter(|p| p["id"].as_str() != Some("mock"))
            .collect()
    }

    pub fn onboarding_connected(&self) -> bool {
        self.onboarding_providers()
            .iter()
            .any(|p| p["connected"].as_bool() == Some(true))
    }

    /// How many selectable rows the current step has.
    pub fn onboarding_rows(&self) -> usize {
        let tour = Tour::of(&self.manifest);
        match tour.current(&self.ui.onboarding).kind.as_str() {
            "provider" => self.onboarding_providers().len(),
            "agents" => tour.agent.len(),
            _ => 0,
        }
    }

    /// Show the tour from its first step (`/onboarding`, Settings > About, and the first run).
    pub fn open_onboarding(&mut self) {
        self.ui.onboarding_offered = true;
        self.ui.onboarding = Onboarding::default();
        self.ui.onboarding_search_asked = false;
        self.ui.view = View::Onboarding;
        self.ui.focus = Focus::Timeline;
        self.ui.overlay = None;
        self.ui.provider_detail = None;
        self.ui.drawer = None;
        self.effects.push(Effect::Sync(SyncCommand::Config));
        self.effects
            .push(Effect::Sync(SyncCommand::Catalog(self.current_project())));
    }

    /// First run: Core says this home has not seen the tour. Called after every config arrival; offers it once per run.
    pub fn offer_onboarding(&mut self) {
        if self.ui.onboarding_offered
            || self.ui.view == View::Onboarding
            || !matches!(self.state.connection, ConnectionState::Live)
            || !should_offer(self.state.config.as_ref())
        {
            return;
        }
        self.open_onboarding();
    }

    /// Leave the tour. Both Finish and Skip mark it done for this Core home; Finish with no project opens Add project.
    pub fn close_onboarding(&mut self, finished: bool) {
        self.ui.view = View::Session;
        self.ui.focus = Focus::Composer;
        self.ui.provider_detail = None;
        self.patch_config_value("ui.onboarded", json!(true));
        if finished && self.current_project().is_none() {
            self.run_command("project.add");
        }
    }

    fn onboarding_step_to(&mut self, step: usize) {
        self.ui.onboarding.step = step;
        self.ui.onboarding.row = 0;
        self.ui.pool_pick = None;
        if self.ui.view == View::Onboarding && Tour::of(&self.manifest).is_search(&self.ui.onboarding)
        {
            self.search_step_entered();
        }
    }

    /// Entering the search step starts SearXNG in the background: ask Core for its state, then start it unless it is
    /// already running or Core says it cannot. A retry after going back and forward starts it again (Core is idempotent).
    fn search_step_entered(&mut self) {
        self.refresh_search_service();
    }

    /// The first status answer for the search step decides whether a start is needed. Never called while another start
    /// is in flight; polling keeps the step live afterwards.
    pub fn maybe_start_onboarding_search(&mut self) {
        if self.ui.onboarding_search_asked
            || self.ui.view != View::Onboarding
            || !Tour::of(&self.manifest).is_search(&self.ui.onboarding)
        {
            return;
        }
        let Some(status) = self.ui.search_service.as_ref() else {
            return;
        };
        if matches!(status["state"].as_str(), Some("running" | "unavailable")) {
            return;
        }
        if self.ui.search_starting {
            return;
        }
        self.ui.onboarding_search_asked = true;
        if status["state"].as_str() != Some("starting") {
            self.start_search_service();
        }
    }

    /// The search step's Retry: start it again, or say why it cannot start.
    pub fn retry_onboarding_search(&mut self) {
        self.ui.onboarding_search_asked = true;
        self.start_search_service();
    }

    /// Enter on the selected row: a provider opens its connection modal, an agent row picks a model or cycles the default agent.
    pub fn onboarding_activate(&mut self) {
        let tour = Tour::of(&self.manifest);
        let row = self.ui.onboarding.row;
        match tour.current(&self.ui.onboarding).kind.as_str() {
            "provider" => {
                let id = self
                    .onboarding_providers()
                    .get(row)
                    .and_then(|p| p["id"].as_str())
                    .map(str::to_owned);
                if let Some(id) = id {
                    self.ui.provider_detail = Some(id);
                    self.ui.provider_detail_row = 0;
                }
            }
            "agents" => {
                let Some(agent) = tour.agent.get(row) else {
                    return;
                };
                if agent.is_root() {
                    let next = next_root_agent(self.state.config.as_ref(), &agent.options);
                    self.patch_config_value("defaults.agent", json!(next));
                } else if !self.onboarding_connected() {
                    self.toast(Level::Warning, tour.no_provider.clone());
                } else {
                    self.ui.onboarding_pick = Some(agent.name.clone());
                    self.open_palette(PaletteMode::OnboardingModels);
                }
            }
            _ => {}
        }
    }

    /// A model chosen in the onboarding picker (`None` clears the agent's model).
    pub fn onboarding_choose_model(&mut self, model: Option<String>) {
        let Some(agent) = self.ui.onboarding_pick.take() else {
            return;
        };
        let patch = agent_models_patch(self.state.config.as_ref(), &agent, model.as_deref());
        self.patch_config_value("agents.models", patch);
    }

    /// Keys while the tour is up. The provider modal and the picker take their own keys first.
    pub fn onboarding_key(&mut self, name: &str, code: crossterm::event::KeyCode) -> bool {
        use crossterm::event::KeyCode;
        if self.ui.provider_detail.is_some() {
            return self.provider_detail_key(name, code);
        }
        let tour = Tour::of(&self.manifest);
        let rows = self.onboarding_rows();
        let last = rows.saturating_sub(1);
        match name {
            "esc" | "left" | "backspace" => {
                if self.ui.onboarding.step > 0 {
                    self.onboarding_step_to(self.ui.onboarding.step - 1);
                }
            }
            "right" | "tab" => {
                if !tour.is_last(&self.ui.onboarding) {
                    self.onboarding_step_to(self.ui.onboarding.step + 1);
                }
            }
            "up" if rows > 0 => self.ui.onboarding.row = self.ui.onboarding.row.saturating_sub(1),
            "down" if rows > 0 => self.ui.onboarding.row = (self.ui.onboarding.row + 1).min(last),
            "enter" => {
                if tour.is_search(&self.ui.onboarding) {
                    self.retry_onboarding_search();
                } else if rows > 0 {
                    self.onboarding_activate();
                } else if tour.is_last(&self.ui.onboarding) {
                    self.close_onboarding(true);
                } else {
                    self.onboarding_step_to(self.ui.onboarding.step + 1);
                }
            }
            _ if code == KeyCode::Char('s') => self.close_onboarding(false),
            _ => return false,
        }
        true
    }

    /// A click on a step's Back, Next, Finish or Skip, or on a row.
    pub fn onboarding_click(&mut self, target: crate::ui::OnboardingTarget) {
        use crate::ui::OnboardingTarget as T;
        let tour = Tour::of(&self.manifest);
        match target {
            T::Skip => self.close_onboarding(false),
            T::Back => {
                if self.ui.onboarding.step > 0 {
                    self.onboarding_step_to(self.ui.onboarding.step - 1);
                }
            }
            T::Next => {
                if tour.is_last(&self.ui.onboarding) {
                    self.close_onboarding(true);
                } else {
                    self.onboarding_step_to(self.ui.onboarding.step + 1);
                }
            }
            T::Row(i) => {
                self.ui.onboarding.row = i;
                self.onboarding_activate();
            }
            T::Retry => self.retry_onboarding_search(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tour() -> (Manifest, Tour) {
        let m = Manifest::load();
        let t = Tour::of(&m);
        (m, t)
    }

    #[test]
    fn manifest_has_a_tour_that_starts_and_ends_right() {
        let (_, t) = tour();
        assert!(t.step.len() >= 5);
        assert_eq!(t.step.first().unwrap().kind, "info");
        assert_eq!(t.step.last().unwrap().kind, "done");
        assert!(t.step.iter().any(|s| s.kind == "provider"));
        assert!(t.step.iter().any(|s| s.kind == "agents"));
        let kinds: Vec<&str> = t.step.iter().map(|s| s.kind.as_str()).collect();
        let search = kinds.iter().position(|k| *k == "search").expect("a search step");
        assert_eq!(search, kinds.iter().position(|k| *k == "agents").unwrap() + 1);
        assert!(t.step[search].lines.iter().any(|l| l.contains("Core")));
        assert!(t.step[search].lines.iter().any(|l| l.contains("image")));
        assert!(!t.hints_search.is_empty());
        assert!(!t.search_retry.is_empty());
    }

    #[test]
    fn scout_and_quick_are_told_to_use_small_models() {
        let (_, t) = tour();
        for name in ["scout", "quick"] {
            let row = t.agent.iter().find(|a| a.name == name).unwrap();
            assert!(
                row.hint.contains("luna")
                    && row.hint.contains("haiku")
                    && row.hint.contains("hydrogen"),
                "{name}: {}",
                row.hint
            );
        }
    }

    #[test]
    fn keys_fill_from_the_manifest_and_nothing_is_left_unfilled() {
        let (m, t) = tour();
        for step in &t.step {
            for line in step.lines.iter().chain(step.tip.iter()) {
                let filled = fill_keys(&m, line);
                assert!(!filled.contains("{key:"), "{filled}");
            }
        }
        assert_eq!(fill_keys(&m, "Press {key:palette.open}."), "Press ctrl+p.");
        // No key: the slash name stands in.
        assert_eq!(
            fill_keys(&m, "Run {key:app.onboarding}."),
            "Run /onboarding."
        );
    }

    #[test]
    fn offered_only_when_the_flag_is_exactly_false() {
        assert!(should_offer(Some(
            &json!({"values": {"ui": {"onboarded": false}}})
        )));
        assert!(!should_offer(Some(
            &json!({"values": {"ui": {"onboarded": true}}})
        )));
        assert!(!should_offer(Some(&json!({"values": {"ui": {}}}))));
        assert!(!should_offer(None));
    }

    #[test]
    fn navigation_stops_at_the_ends() {
        let (_, t) = tour();
        let mut s = Onboarding::default();
        assert!(!t.is_last(&s));
        s.step = t.step.len() - 1;
        assert!(t.is_last(&s));
        assert_eq!(t.current(&s).kind, "done");
    }

    #[test]
    fn a_model_choice_touches_only_its_own_agent() {
        let config =
            json!({"values": {"agents": {"models": {"scout": ["a/x"], "plan": ["b/y", "c/z"]}}}});
        let patch = agent_models_patch(Some(&config), "quick", Some("a/luna"));
        assert_eq!(patch["quick"], json!(["a/luna"]));
        assert_eq!(patch["scout"], json!(["a/x"]));
        assert_eq!(patch["plan"], json!(["b/y", "c/z"]));
        let cleared = agent_models_patch(Some(&config), "scout", None);
        assert!(cleared.get("scout").is_none());
        assert_eq!(agent_model_line(Some(&config), "plan"), "b/y +1");
        assert_eq!(
            agent_model_line(Some(&config), "quick"),
            "the session's model"
        );
    }

    #[test]
    fn the_root_agent_cycles_through_its_options() {
        let options = vec!["build".to_owned(), "orchestrate".to_owned()];
        assert_eq!(root_agent(None, &options), "build");
        let config = json!({"values": {"defaults": {"agent": "orchestrate"}}});
        assert_eq!(root_agent(Some(&config), &options), "orchestrate");
        assert_eq!(next_root_agent(Some(&config), &options), "build");
        assert_eq!(next_root_agent(None, &options), "orchestrate");
    }
}
