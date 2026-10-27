//! The mutations the shell can make through Core's HTTP API, and how they run.
//! Each user action is one `ApiCall` with its own `RequestId`; the result comes
//! back as an `ActionDone` that the app turns into a toast (and, for a failed
//! send, a restored draft).

use std::path::Path;

use serde_json::json;

use crate::api::{CoreApi, Outcome, RequestId};
use crate::discovery;
use crate::transport::Http;

#[derive(Debug, Clone, PartialEq)]
pub enum ApiCall {
    /// Send a prompt to an existing idle session.
    Prompt {
        session: String,
        text: String,
    },
    /// Create a session in a project, then prompt it.
    NewAndPrompt {
        project: String,
        text: String,
    },
    NewSession {
        project: String,
    },
    Continue {
        session: String,
    },
    Interrupt {
        session: String,
    },
    Archive {
        session: String,
        archived: bool,
    },
    Rename {
        session: String,
        title: String,
        revision: i64,
    },
    Delete {
        session: String,
    },
    Reply {
        session: String,
        id: String,
        allow: bool,
    },
    /// Apply from the next model call; an active call keeps its configuration.
    SetModel {
        session: String,
        model: String,
        revision: i64,
    },
    SetAgent {
        session: String,
        agent: String,
        revision: i64,
    },
    /// Empty clears the session effort (the agent's or provider's applies).
    SetEffort {
        session: String,
        effort: String,
        revision: i64,
    },
    /// Write a Markdown transcript under `<Nexus home>/exports`.
    Export {
        session: String,
        format: String,
        scope: String,
    },
    ChangeDirectory {
        session: String,
        directory: String,
    },
}

impl ApiCall {
    /// Past-tense label for the success toast.
    pub fn done_label(&self) -> &'static str {
        match self {
            ApiCall::Prompt { .. } | ApiCall::NewAndPrompt { .. } => "Sent",
            ApiCall::NewSession { .. } => "Session created",
            ApiCall::Continue { .. } => "Continuing",
            ApiCall::Interrupt { .. } => "Interrupt sent",
            ApiCall::Archive { archived: true, .. } => "Archived",
            ApiCall::Archive { .. } => "Unarchived",
            ApiCall::Rename { .. } => "Renamed",
            ApiCall::Delete { .. } => "Deleted",
            ApiCall::Reply { allow: true, .. } => "Allowed",
            ApiCall::Reply { .. } => "Denied",
            ApiCall::SetModel { .. } => "Model changed from the next call",
            ApiCall::SetAgent { .. } => "Agent changed from the next call",
            ApiCall::SetEffort { .. } => "Effort changed from the next call",
            ApiCall::Export { .. } => "Exported",
            ApiCall::ChangeDirectory { .. } => "Directory change requested",
        }
    }

    /// Text to put back in the composer if the call fails.
    pub fn draft(&self) -> Option<(Option<&str>, &str)> {
        match self {
            ApiCall::Prompt { session, text } => Some((Some(session), text)),
            ApiCall::NewAndPrompt { text, .. } => Some((None, text)),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ActionDone {
    pub call: ApiCall,
    pub result: Result<Done, String>,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Done {
    /// A session the app should open (new sessions).
    pub open: Option<String>,
    /// Set when the write lost a revision race; the app reports it.
    pub conflict: Option<String>,
    /// A local file the action produced (exports), for the success toast.
    pub path: Option<String>,
}

/// An authenticated API for the daemon in `home`, from `daemon.json`.
pub fn api_for(home: &Path) -> Option<CoreApi> {
    let info = discovery::read_info(home)?;
    Http::new(&info.url, &info.token).ok().map(CoreApi::new)
}

fn conflict<T>(o: Outcome<T>) -> Result<Option<T>, String> {
    match o {
        Outcome::Done { value, .. } => Ok(Some(value)),
        Outcome::Conflict { message, .. } => Err(format!(
            "Changed elsewhere ({message}); the view is current, try again"
        )),
    }
}

pub async fn run(api: Option<CoreApi>, call: ApiCall) -> ActionDone {
    let result = match api {
        None => Err("Core is not reachable".to_owned()),
        Some(api) => execute(&api, &call).await,
    };
    ActionDone { call, result }
}

async fn execute(api: &CoreApi, call: &ApiCall) -> Result<Done, String> {
    let e = |e: crate::transport::Error| e.to_string();
    let key = RequestId::new();
    let mut done = Done::default();
    match call {
        ApiCall::Prompt { session, text } => {
            conflict(api.prompt(session, text, &key).await.map_err(e)?)?;
        }
        ApiCall::NewAndPrompt { project, text } => {
            let s = conflict(
                api.create_session(project, json!({}), &key)
                    .await
                    .map_err(e)?,
            )?
            .ok_or("no session returned")?;
            done.open = Some(s.id.clone());
            let key2 = RequestId::new();
            conflict(api.prompt(&s.id, text, &key2).await.map_err(e)?)?;
        }
        ApiCall::NewSession { project } => {
            let s = conflict(
                api.create_session(project, json!({}), &key)
                    .await
                    .map_err(e)?,
            )?
            .ok_or("no session returned")?;
            done.open = Some(s.id);
        }
        ApiCall::Continue { session } => {
            conflict(api.continue_turn(session, &key).await.map_err(e)?)?;
        }
        ApiCall::Interrupt { session } => {
            conflict(api.interrupt(session, &key).await.map_err(e)?)?;
        }
        ApiCall::Archive { session, archived } => {
            conflict(
                api.set_archived(session, *archived, &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::Rename {
            session,
            title,
            revision,
        } => {
            match api
                .patch_session(session, json!({ "title": title }), Some(*revision), &key)
                .await
                .map_err(e)?
            {
                Outcome::Done { .. } => {}
                Outcome::Conflict { message, .. } => done.conflict = Some(message),
            }
        }
        ApiCall::Delete { session } => {
            conflict(api.delete_session(session, &key).await.map_err(e)?)?;
        }
        ApiCall::Reply { session, id, allow } => {
            conflict(
                api.reply_permission(session, id, *allow, &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::SetModel {
            session,
            model,
            revision,
        } => match api
            .set_model_rev(session, model, Some(*revision), &key)
            .await
            .map_err(e)?
        {
            Outcome::Done { .. } => {}
            Outcome::Conflict { message, .. } => done.conflict = Some(message),
        },
        ApiCall::SetAgent {
            session,
            agent,
            revision,
        } => match api
            .set_agent_rev(session, agent, Some(*revision), &key)
            .await
            .map_err(e)?
        {
            Outcome::Done { .. } => {}
            Outcome::Conflict { message, .. } => done.conflict = Some(message),
        },
        ApiCall::ChangeDirectory { session, directory } => {
            conflict(
                api.change_directory(session, directory, &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::Export {
            session,
            format,
            scope,
        } => {
            let ext = if format == "json" { "json" } else { "md" };
            let (text, _name) = api.export(session, format, scope).await.map_err(e)?;
            let dir = crate::discovery::home()
                .unwrap_or_else(|_| std::env::temp_dir())
                .join("exports");
            std::fs::create_dir_all(&dir)
                .map_err(|err| format!("could not create {}: {err}", dir.display()))?;
            let stamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0, |d| d.as_secs());
            let path = dir.join(format!("{session}-{scope}-{stamp}.{ext}"));
            std::fs::write(&path, text)
                .map_err(|err| format!("could not write {}: {err}", path.display()))?;
            done.path = Some(path.display().to_string());
        }
        ApiCall::SetEffort {
            session,
            effort,
            revision,
        } => match api
            .patch_session(session, json!({ "effort": effort }), Some(*revision), &key)
            .await
            .map_err(e)?
        {
            Outcome::Done { .. } => {}
            Outcome::Conflict { message, .. } => done.conflict = Some(message),
        },
    }
    Ok(done)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_sends_hand_the_draft_back() {
        let c = ApiCall::Prompt {
            session: "ses_1".into(),
            text: "hello".into(),
        };
        assert_eq!(c.draft(), Some((Some("ses_1"), "hello")));
        assert_eq!(
            ApiCall::Interrupt {
                session: "s".into()
            }
            .draft(),
            None
        );
    }

    #[tokio::test]
    async fn no_daemon_is_an_error_not_a_panic() {
        let done = run(
            None,
            ApiCall::Continue {
                session: "s".into(),
            },
        )
        .await;
        assert!(done.result.is_err());
    }
}
