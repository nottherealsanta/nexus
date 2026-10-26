//! Native launch: `NEXUS_URL` and `NEXUS_TICKET` from the environment.

use super::{Launch, MISSING_ENV, launch_from_url};

pub fn launch() -> Result<Launch, String> {
    let url = std::env::var("NEXUS_URL").map_err(|_| MISSING_ENV.to_owned())?;
    launch_from_url(&url, std::env::var("NEXUS_TICKET").ok())
}
