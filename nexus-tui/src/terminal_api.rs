//! HTTP adapter for Core-owned terminal resources.
use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::Method;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::transport::{Error, EventStream, Http};

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct TerminalResource {
    pub id: String,
    pub name: String,
    pub workspace: String,
    pub status: String,
    pub controller: Option<String>,
    pub rows: u16,
    pub cols: u16,
}

#[derive(Clone)]
pub struct TerminalApi(pub Http);

impl TerminalApi {
    async fn call(&self, method: Method, path: &str, body: Option<Value>) -> Result<Value, Error> {
        Ok(self
            .0
            .call(method, path, &[], body.as_ref(), None, None)
            .await?
            .body)
    }

    fn decode<T: serde::de::DeserializeOwned>(value: Value) -> Result<T, Error> {
        serde_json::from_value(value).map_err(|e| Error::Decode(e.to_string()))
    }

    pub async fn list(&self, project: &str) -> Result<Vec<TerminalResource>, Error> {
        Self::decode(
            self.0
                .get("/api/terminal", &[("project_id", project.into())])
                .await?,
        )
    }

    pub async fn create(
        &self,
        project: &str,
        workspace: &str,
        session: Option<&str>,
        client: &str,
        rows: u16,
        cols: u16,
    ) -> Result<TerminalResource, Error> {
        Self::decode(self.call(Method::POST, "/api/terminal", Some(json!({"project_id":project,"workspace":workspace,"session_id":session,"client_id":client,"rows":rows,"cols":cols}))).await?)
    }

    pub async fn attach(&self, id: &str, client: &str) -> Result<String, Error> {
        let value = self
            .call(
                Method::POST,
                &format!("/api/terminal/{id}/attach"),
                Some(json!({"client_id":client})),
            )
            .await?;
        value
            .get("attachment_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| Error::Decode("missing attachment id".into()))
    }

    pub async fn stream(
        &self,
        id: &str,
        attachment: &str,
        after: Option<u64>,
    ) -> Result<EventStream, Error> {
        let mut query = vec![("attachment_id", attachment.to_owned())];
        if let Some(seq) = after {
            query.push(("after", seq.to_string()));
        }
        self.0
            .sse(&format!("/api/terminal/{id}/stream"), &query)
            .await
    }

    async fn action(&self, id: &str, action: &str, body: Value) -> Result<Value, Error> {
        self.call(
            Method::POST,
            &format!("/api/terminal/{id}/{action}"),
            Some(body),
        )
        .await
    }

    pub async fn control(
        &self,
        id: &str,
        attachment: &str,
        acquire: bool,
    ) -> Result<TerminalResource, Error> {
        Self::decode(self.action(id, "control", json!({"attachment_id":attachment,"action":if acquire {"acquire"} else {"release"}})).await?)
    }

    pub async fn input(
        &self,
        id: &str,
        attachment: &str,
        seq: u64,
        bytes: &[u8],
    ) -> Result<u64, Error> {
        let result = self
            .action(
                id,
                "input",
                json!({"attachment_id":attachment,"input_seq":seq,"data":STANDARD.encode(bytes)}),
            )
            .await?;
        result
            .get("input_seq")
            .and_then(Value::as_u64)
            .filter(|ack| *ack == seq)
            .ok_or_else(|| Error::Decode("invalid input acknowledgement".into()))
    }

    pub async fn resize(
        &self,
        id: &str,
        attachment: &str,
        rows: u16,
        cols: u16,
    ) -> Result<TerminalResource, Error> {
        Self::decode(
            self.action(
                id,
                "resize",
                json!({"attachment_id":attachment,"rows":rows,"cols":cols}),
            )
            .await?,
        )
    }

    pub async fn detach(&self, id: &str, attachment: &str) -> Result<(), Error> {
        self.action(id, "detach", json!({"attachment_id":attachment}))
            .await
            .map(|_| ())
    }

    pub async fn rename(&self, id: &str, name: &str) -> Result<TerminalResource, Error> {
        Self::decode(
            self.call(
                Method::PATCH,
                &format!("/api/terminal/{id}"),
                Some(json!({"name":name})),
            )
            .await?,
        )
    }

    pub async fn close(&self, id: &str) -> Result<TerminalResource, Error> {
        Self::decode(
            self.call(Method::DELETE, &format!("/api/terminal/{id}"), None)
                .await?,
        )
    }
}
