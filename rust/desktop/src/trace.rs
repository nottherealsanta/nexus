//! Opt-in bounded desktop timings (DESKTOP_OVERHAUL_PLAN §3.6).
//! CPU element phases are measured separately from display presentation/GPU work.
use gpui::*;
use std::{
    cell::RefCell,
    collections::{BTreeMap, VecDeque},
    rc::Rc,
    time::{Duration, Instant},
};

pub(crate) type SharedTrace = Rc<RefCell<Trace>>;
#[derive(Default)]
pub(crate) struct Trace {
    enabled: bool,
    samples: VecDeque<(&'static str, f64)>,
    published: Option<Instant>,
    input: Option<Instant>,
}
impl Trace {
    pub(crate) fn new() -> SharedTrace {
        Rc::new(RefCell::new(Self {
            enabled: std::env::var("NEXUS_DESKTOP_TRACE").as_deref() == Ok("1"),
            ..Self::default()
        }))
    }
    pub(crate) fn enabled(&self) -> bool {
        self.enabled
    }
    pub(crate) fn input(&mut self) {
        if self.enabled {
            self.input.get_or_insert_with(Instant::now);
        }
    }
    pub(crate) fn record(&mut self, name: &'static str, elapsed: Duration) {
        self.sample(name, elapsed.as_secs_f64() * 1000.);
    }
    pub(crate) fn sample(&mut self, name: &'static str, value: f64) {
        if !self.enabled {
            return;
        }
        if self.samples.len() == 4096 {
            self.samples.pop_front();
        }
        self.samples.push_back((name, value));
    }
    fn summary(&self) -> Vec<String> {
        let mut groups: BTreeMap<&str, Vec<f64>> = BTreeMap::new();
        for (name, value) in &self.samples {
            groups.entry(name).or_default().push(*value);
        }
        groups
            .into_iter()
            .map(|(name, mut values)| {
                values.sort_by(f64::total_cmp);
                let at = |p: f64| values[((values.len() - 1) as f64 * p).ceil() as usize];
                let unit = if name == "snapshot_bytes" {
                    "bytes"
                } else {
                    "ms"
                };
                format!(
                    "Desktop trace {name}: p50 {:.3} p95 {:.3} max {:.3} {unit} (n={})",
                    at(0.5),
                    at(0.95),
                    values[values.len() - 1],
                    values.len()
                )
            })
            .collect()
    }
    fn painted(&mut self) {
        if !self.enabled {
            return;
        }
        if let Some(at) = self.input.take() {
            self.record("input_to_cpu_paint", at.elapsed());
        }
        if self
            .published
            .is_none_or(|at| at.elapsed() >= Duration::from_secs(2))
        {
            self.published = Some(Instant::now());
            for line in self.summary() {
                eprintln!("{line}");
            }
        }
    }
}

/// Wrap the actual root, so phase timings include all visible descendant elements.
pub(crate) struct TracedElement {
    child: AnyElement,
    trace: SharedTrace,
}
impl TracedElement {
    pub(crate) fn new(child: impl IntoElement, trace: SharedTrace) -> Self {
        Self {
            child: child.into_any_element(),
            trace,
        }
    }
}
impl IntoElement for TracedElement {
    type Element = Self;
    fn into_element(self) -> Self {
        self
    }
}
impl Element for TracedElement {
    type RequestLayoutState = ();
    type PrepaintState = ();
    fn id(&self) -> Option<ElementId> {
        None
    }
    fn source_location(&self) -> Option<&'static std::panic::Location<'static>> {
        None
    }
    fn request_layout(
        &mut self,
        _: Option<&GlobalElementId>,
        _: Option<&InspectorElementId>,
        window: &mut Window,
        cx: &mut App,
    ) -> (LayoutId, ()) {
        let at = Instant::now();
        let id = self.child.request_layout(window, cx);
        self.trace
            .borrow_mut()
            .record("request_layout", at.elapsed());
        (id, ())
    }
    fn prepaint(
        &mut self,
        _: Option<&GlobalElementId>,
        _: Option<&InspectorElementId>,
        _: Bounds<Pixels>,
        _: &mut (),
        window: &mut Window,
        cx: &mut App,
    ) {
        let at = Instant::now();
        self.child.prepaint(window, cx);
        self.trace.borrow_mut().record("prepaint", at.elapsed());
    }
    fn paint(
        &mut self,
        _: Option<&GlobalElementId>,
        _: Option<&InspectorElementId>,
        _: Bounds<Pixels>,
        _: &mut (),
        _: &mut (),
        window: &mut Window,
        cx: &mut App,
    ) {
        let at = Instant::now();
        self.child.paint(window, cx);
        let mut trace = self.trace.borrow_mut();
        trace.record("paint", at.elapsed());
        trace.painted();
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;
    #[test]
    fn trace_is_bounded_and_disabled_by_default() {
        let mut trace = Trace {
            enabled: true,
            ..Trace::default()
        };
        for n in 0..5000 {
            trace.sample("snapshot_bytes", n as f64);
        }
        assert_eq!(trace.samples.len(), 4096);
        let summary = trace.summary();
        assert!(summary[0].contains("p95 4795.000"));
        assert!(summary[0].contains("bytes (n=4096)"));
        let mut disabled = Trace::default();
        disabled.input();
        disabled.sample("paint", 1.);
        assert!(disabled.samples.is_empty());
        assert!(disabled.input.is_none());
    }
}
