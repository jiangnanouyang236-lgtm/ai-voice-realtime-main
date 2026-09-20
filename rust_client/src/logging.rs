use std::{env, io::IsTerminal};
use tracing_subscriber::{fmt, EnvFilter};

pub fn init_tracing() {
    let filter = env::var("VOICE_LOG_LEVEL")
        .or_else(|_| env::var("RUST_LOG"))
        .unwrap_or_else(|_| "info".to_string());
    let filter = EnvFilter::try_new(filter).unwrap_or_else(|_| EnvFilter::new("info"));
    let format = env::var("VOICE_LOG_FORMAT")
        .or_else(|_| env::var("RUST_LOG_FORMAT"))
        .unwrap_or_else(|_| "compact".to_string())
        .to_ascii_lowercase();
    let ansi = match env::var("VOICE_LOG_COLOR")
        .unwrap_or_else(|_| "auto".to_string())
        .to_ascii_lowercase()
        .as_str()
    {
        "1" | "true" | "yes" | "on" | "always" => true,
        "0" | "false" | "no" | "off" | "never" => false,
        _ => std::io::stderr().is_terminal(),
    };

    match format.as_str() {
        "pretty" => fmt()
            .with_env_filter(filter)
            .with_target(false)
            .with_ansi(ansi)
            .pretty()
            .init(),
        "full" => fmt()
            .with_env_filter(filter)
            .with_target(true)
            .with_ansi(ansi)
            .init(),
        _ => fmt()
            .with_env_filter(filter)
            .with_target(false)
            .with_thread_ids(false)
            .with_thread_names(false)
            .with_ansi(ansi)
            .compact()
            .init(),
    }
}
