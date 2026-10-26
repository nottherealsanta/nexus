//! UI components. Each file owns one region; they read [`crate::store::Store`]
//! signals and call its methods, nothing else touches the network.

pub mod composer;
pub mod sidebar;
pub mod timeline;
pub mod topbar;
