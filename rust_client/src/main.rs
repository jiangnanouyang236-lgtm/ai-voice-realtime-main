mod app;
mod audio;
mod audio_control;
mod audio_frontend;
mod barge_in;
mod config;
mod environment_trigger;
mod gateway;
mod logging;
mod protocol;
#[cfg(any(feature = "native-webrtc", test))]
mod snapshot;
mod state;
mod transport;
mod tts;
mod vad;
mod wake;

use anyhow::Result;
use app::App;
use config::{env_help_text, Config};
use tokio::sync::watch;
use tracing::{error, info};

async fn wait_for_shutdown_signal() -> Result<&'static str> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => {
                result?;
                Ok("SIGINT")
            }
            _ = terminate.recv() => Ok("SIGTERM"),
        }
    }

    #[cfg(not(unix))]
    {
        tokio::signal::ctrl_c().await?;
        Ok("Ctrl+C")
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let print_env_help = std::env::args()
        .skip(1)
        .any(|arg| arg == "--print-env-help");
    if print_env_help {
        print!("{}", env_help_text());
        return Ok(());
    }

    let print_config = std::env::args().skip(1).any(|arg| arg == "--print-config");
    if print_config {
        let config = Config::from_env()?;
        serde_json::to_writer_pretty(std::io::stdout(), &config.printable())?;
        println!();
        return Ok(());
    }

    logging::init_tracing();
    let config = Config::from_env()?;
    let mut app = App::new(config);
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    tokio::spawn(async move {
        match wait_for_shutdown_signal().await {
            Ok(signal) => {
                info!(signal, "[SHUTDOWN] 收到退出信号，开始优雅退出");
                let _ = shutdown_tx.send(true);
            }
            Err(err) => {
                error!(error = %err, "[SHUTDOWN] 退出信号监听失败");
            }
        }
    });
    app.run_forever(shutdown_rx).await
}
