//! Shared Mermaid rendering for Nexus clients.
//!
//! - [`text`] rebuilds pie, Gantt and timeline charts as legible rows.
//! - `svg` (feature `svg`) compiles every supported diagram to SVG through
//!   Ondine, with bounded input/output and links and images disabled.
//! - `raster` (feature `raster`) turns the same SVG into a PNG with the
//!   bundled Monaspace Argon, so every client can show the same artifact.

pub mod text;

#[cfg(feature = "svg")]
pub mod svg;

#[cfg(feature = "raster")]
pub mod raster;
