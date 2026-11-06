use std::{
    env,
    path::{Path, PathBuf},
    process::ExitCode,
};

use nsh_platform_gpui::{AppOptions, AppSource, AssetBase, Bundle, Limits, run_app, set_ui_sets};

fn main() -> ExitCode {
    let Some(path) = env::args_os().nth(1).map(PathBuf::from) else {
        eprintln!("usage: nexus-gpui <bundle.js>");
        return ExitCode::FAILURE;
    };
    let bundle = match load_bundle(&path) {
        Ok(bundle) => bundle,
        Err(error) => {
            eprintln!("cannot read {}: {error}", path.display());
            return ExitCode::FAILURE;
        }
    };
    let options = AppOptions {
        title: "Nexus (GPUI spike)".into(),
        app_id: "dev.nexus10.nsh".into(),
        asset_base: path
            .parent()
            .map(|dir| AssetBase::Directory(dir.join("assets"))),
        limits: Limits::standalone(),
        ..AppOptions::default()
    };
    set_ui_sets(vec![nsh_base::registry(), nexus_nsh_native::registry()]);
    run_app(AppSource::Bundle(bundle), options);
    ExitCode::SUCCESS
}

fn load_bundle(path: &Path) -> std::io::Result<Bundle> {
    // Bundle::source drops the build's capabilities, including the daemon file grant.
    std::fs::metadata(path.with_file_name("capabilities.json"))?;
    Bundle::read(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundle_loads_filesystem_grants_and_requires_metadata() {
        let dir = std::env::temp_dir().join(format!("nexus-gpui-bundle-{}", std::process::id()));
        std::fs::create_dir(&dir).unwrap();
        let path = dir.join("bundle.js");
        std::fs::write(&path, "").unwrap();
        assert!(load_bundle(&path).is_err());
        std::fs::write(
            dir.join("capabilities.json"),
            r#"{"fs":{"read":["${homeDir}/.nexus"]}}"#,
        )
        .unwrap();
        let bundle = load_bundle(&path).unwrap();
        let caps = bundle.policy.declared.unwrap();
        assert_eq!(caps.fs.read.len(), 1);
        assert!(caps.fs.write.is_empty());
        std::fs::remove_file(dir.join("capabilities.json")).unwrap();
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir(dir).unwrap();
    }
}
