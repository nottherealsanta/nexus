//! Recorder spool re-export.
//!
//! The durable spool, recovery plan and lock protocol live in the shared
//! `nexus-recorder-spool` crate so NSH desktop can use the same on-disk format.
//! TUI call sites keep using `crate::recorder_spool::…`.

pub use nexus_recorder_spool::*;
