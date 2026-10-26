//! Web launch: relative API under the page's own origin; the browser holds the
//! cookie and sets `Origin`.

use super::Launch;

pub fn launch() -> Result<Launch, String> {
    Ok(Launch { base: "../v1/web/".to_owned(), origin: None, ticket: None })
}
