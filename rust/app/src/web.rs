use serde_json::Value;
use wasm_bindgen::{JsCast, closure::Closure};
use wasm_bindgen_futures::JsFuture;
thread_local! { pub static STREAM: std::cell::RefCell<Option<(web_sys::EventSource,Closure<dyn FnMut(web_sys::MessageEvent)>)>> = const { std::cell::RefCell::new(None) }; }

pub fn base() -> String {
    let path = web_sys::window()
        .unwrap()
        .location()
        .pathname()
        .unwrap_or_default();
    if path.starts_with("/w/") {
        format!("/w/{}", path.split('/').nth(2).unwrap_or(""))
    } else {
        String::new()
    }
}
pub async fn api(path: &str, body: Option<Value>, csrf: &str) -> Result<Value, String> {
    let init = web_sys::RequestInit::new();
    init.set_credentials(web_sys::RequestCredentials::SameOrigin);
    init.set_method(if body.is_some() { "POST" } else { "GET" });
    if let Some(v) = body {
        init.set_body(&wasm_bindgen::JsValue::from_str(&v.to_string()));
    }
    let req = web_sys::Request::new_with_str_and_init(&format!("{}/v1/web/{path}", base()), &init)
        .map_err(|_| "request failed")?;
    req.headers()
        .set("Content-Type", "application/json")
        .map_err(|_| "headers failed")?;
    if !csrf.is_empty() {
        req.headers()
            .set("X-CSRF-Token", csrf)
            .map_err(|_| "headers failed")?;
    }
    let response = JsFuture::from(web_sys::window().unwrap().fetch_with_request(&req))
        .await
        .map_err(|_| "network error")?
        .dyn_into::<web_sys::Response>()
        .map_err(|_| "response failed")?;
    if !response.ok() {
        return Err(format!("HTTP {}", response.status()));
    }
    let text = JsFuture::from(response.text().map_err(|_| "response failed")?)
        .await
        .map_err(|_| "response failed")?
        .as_string()
        .unwrap_or_default();
    let value: Value = serde_json::from_str(&text).map_err(|_| "invalid response")?;
    if value["type"] == "ErrorResult" {
        return Err(value["message"].as_str().unwrap_or("Command failed").into());
    }
    Ok(value)
}
