use crate::config::AudioControlConfig;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::{
    sync::{
        atomic::{AtomicU32, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::JoinHandle,
    time,
};
use tracing::{error, info, warn};

#[derive(Clone)]
pub struct AudioDuckingState {
    factor_bits: Arc<AtomicU32>,
    meta: Arc<Mutex<DuckingMeta>>,
    default_ttl_ms: u64,
    max_ttl_ms: u64,
}

#[derive(Debug, Default)]
struct DuckingMeta {
    active: bool,
    expires_at: Option<Instant>,
}

#[derive(Debug, Deserialize)]
struct DuckRequest {
    volume: f32,
    ttl_ms: Option<u64>,
}

#[derive(Debug, Serialize)]
struct DuckResponse {
    active: bool,
    volume: f32,
    remaining_ms: Option<u64>,
    default_ttl_ms: u64,
    max_ttl_ms: u64,
}

struct HttpRequest {
    method: String,
    path: String,
    body: Vec<u8>,
}

impl AudioDuckingState {
    pub fn new(default_ttl_ms: u64, max_ttl_ms: u64) -> Self {
        Self {
            factor_bits: Arc::new(AtomicU32::new(1.0f32.to_bits())),
            meta: Arc::new(Mutex::new(DuckingMeta::default())),
            default_ttl_ms,
            max_ttl_ms,
        }
    }

    pub fn current_factor(&self) -> f32 {
        f32::from_bits(self.factor_bits.load(Ordering::Relaxed)).clamp(0.0, 1.0)
    }

    fn duck(&self, requested_volume: f32, requested_ttl_ms: Option<u64>) -> DuckResponse {
        let volume = if requested_volume.is_finite() {
            requested_volume.clamp(0.0, 1.0)
        } else {
            1.0
        };
        let ttl_ms = requested_ttl_ms
            .unwrap_or(self.default_ttl_ms)
            .clamp(1, self.max_ttl_ms);
        let expires_at = Instant::now() + Duration::from_millis(ttl_ms);

        self.factor_bits.store(volume.to_bits(), Ordering::Relaxed);
        let mut meta = self.meta.lock().unwrap();
        meta.active = true;
        meta.expires_at = Some(expires_at);

        info!(
            "[AUDIO] ducking 生效: volume={:.2}, ttl_ms={}",
            volume, ttl_ms
        );
        self.status_from_meta(&meta)
    }

    fn restore(&self, reason: &'static str) -> DuckResponse {
        self.factor_bits.store(1.0f32.to_bits(), Ordering::Relaxed);
        let mut meta = self.meta.lock().unwrap();
        let was_active = meta.active;
        meta.active = false;
        meta.expires_at = None;
        if was_active {
            info!("[AUDIO] ducking 已恢复: reason={}", reason);
        }
        self.status_from_meta(&meta)
    }

    fn restore_if_expired(&self) {
        let should_restore = {
            let meta = self.meta.lock().unwrap();
            meta.active
                && meta
                    .expires_at
                    .map(|expires_at| Instant::now() >= expires_at)
                    .unwrap_or(false)
        };

        if should_restore {
            let _ = self.restore("ttl_expired");
        }
    }

    fn status(&self) -> DuckResponse {
        self.restore_if_expired();
        let meta = self.meta.lock().unwrap();
        self.status_from_meta(&meta)
    }

    fn status_from_meta(&self, meta: &DuckingMeta) -> DuckResponse {
        let remaining_ms = if meta.active {
            meta.expires_at.map(|expires_at| {
                expires_at
                    .saturating_duration_since(Instant::now())
                    .as_millis() as u64
            })
        } else {
            None
        };

        DuckResponse {
            active: meta.active,
            volume: self.current_factor(),
            remaining_ms,
            default_ttl_ms: self.default_ttl_ms,
            max_ttl_ms: self.max_ttl_ms,
        }
    }
}

pub fn spawn_audio_control_server(
    config: AudioControlConfig,
    state: AudioDuckingState,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let watchdog_state = state.clone();
        tokio::spawn(async move {
            let mut interval = time::interval(Duration::from_millis(200));
            loop {
                interval.tick().await;
                watchdog_state.restore_if_expired();
            }
        });

        if let Err(err) = run_server(config, state).await {
            error!(error = %err, "[AUDIO][ERROR] ducking 控制服务退出");
        }
    })
}

async fn run_server(config: AudioControlConfig, state: AudioDuckingState) -> Result<()> {
    let addr = format!("{}:{}", config.host, config.port);
    let listener = TcpListener::bind(&addr).await?;
    info!("[AUDIO] ducking 控制服务已启动: http://{}", addr);

    loop {
        let (stream, peer) = listener.accept().await?;
        let state = state.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_connection(stream, state).await {
                warn!(peer = %peer, error = %err, "[AUDIO][WARN] ducking 控制请求处理失败");
            }
        });
    }
}

async fn handle_connection(mut stream: TcpStream, state: AudioDuckingState) -> Result<()> {
    let request = read_http_request(&mut stream).await?;
    match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/audio/duck") => {
            write_json_response(&mut stream, 200, "OK", &state.status()).await?;
        }
        ("POST", "/audio/duck") => {
            let request: DuckRequest = match serde_json::from_slice(&request.body) {
                Ok(request) => request,
                Err(err) => {
                    write_plain_response(
                        &mut stream,
                        400,
                        "Bad Request",
                        &format!("invalid json: {err}"),
                    )
                    .await?;
                    return Ok(());
                }
            };
            let response = state.duck(request.volume, request.ttl_ms);
            write_json_response(&mut stream, 200, "OK", &response).await?;
        }
        ("POST", "/audio/restore") => {
            let response = state.restore("manual_restore");
            write_json_response(&mut stream, 200, "OK", &response).await?;
        }
        _ => {
            write_plain_response(&mut stream, 404, "Not Found", "not found").await?;
        }
    }
    Ok(())
}

async fn read_http_request(stream: &mut TcpStream) -> Result<HttpRequest> {
    let mut buffer = Vec::with_capacity(4096);
    let mut temp = [0u8; 1024];

    let header_end = loop {
        let n = time::timeout(Duration::from_secs(2), stream.read(&mut temp)).await??;
        if n == 0 {
            anyhow::bail!("connection closed before request headers");
        }
        buffer.extend_from_slice(&temp[..n]);
        if let Some(pos) = find_header_end(&buffer) {
            break pos;
        }
        if buffer.len() > 64 * 1024 {
            anyhow::bail!("request headers too large");
        }
    };

    let headers = String::from_utf8_lossy(&buffer[..header_end]);
    let mut lines = headers.lines();
    let request_line = lines.next().unwrap_or_default();
    let mut request_parts = request_line.split_whitespace();
    let method = request_parts.next().unwrap_or_default().to_string();
    let path = request_parts.next().unwrap_or_default().to_string();
    if method.is_empty() || path.is_empty() {
        anyhow::bail!("invalid request line");
    }

    let mut content_length = 0usize;
    for line in lines {
        if let Some((key, value)) = line.split_once(':') {
            if key.trim().eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse::<usize>().unwrap_or(0);
            }
        }
    }

    let body_start = header_end + 4;
    let mut body = buffer[body_start..].to_vec();
    while body.len() < content_length {
        let n = time::timeout(Duration::from_secs(2), stream.read(&mut temp)).await??;
        if n == 0 {
            anyhow::bail!("connection closed before request body");
        }
        body.extend_from_slice(&temp[..n]);
        if body.len() > 64 * 1024 {
            anyhow::bail!("request body too large");
        }
    }
    body.truncate(content_length);

    Ok(HttpRequest { method, path, body })
}

fn find_header_end(buffer: &[u8]) -> Option<usize> {
    buffer.windows(4).position(|window| window == b"\r\n\r\n")
}

async fn write_json_response<T: Serialize>(
    stream: &mut TcpStream,
    status: u16,
    reason: &str,
    body: &T,
) -> Result<()> {
    let body = serde_json::to_vec(body)?;
    write_response(stream, status, reason, "application/json", &body).await
}

async fn write_plain_response(
    stream: &mut TcpStream,
    status: u16,
    reason: &str,
    body: &str,
) -> Result<()> {
    write_response(
        stream,
        status,
        reason,
        "text/plain; charset=utf-8",
        body.as_bytes(),
    )
    .await
}

async fn write_response(
    stream: &mut TcpStream,
    status: u16,
    reason: &str,
    content_type: &str,
    body: &[u8],
) -> Result<()> {
    let header = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        status,
        reason,
        content_type,
        body.len()
    );
    stream.write_all(header.as_bytes()).await?;
    stream.write_all(body).await?;
    stream.shutdown().await?;
    Ok(())
}
