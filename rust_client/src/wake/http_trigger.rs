use crate::config::WakeConfig;
use crate::wake::serial_monitor::{SerialWakeEvent, WakeFields};
use anyhow::Result;
use crossbeam::channel::{unbounded, Receiver, Sender, TryRecvError};
use serde::{Deserialize, Serialize};
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::JoinHandle,
    time,
};
use tracing::{error, info, warn};

const HTTP_WAKE_SOURCE: &str = "http_wakeup";

#[derive(Debug, Deserialize)]
struct HttpWakePayload {
    keyword: Option<String>,
    angle: Option<String>,
    beamforming: Option<String>,
    raw: Option<String>,
}

#[derive(Debug, Serialize)]
struct HttpWakeResponse {
    ok: bool,
    status: &'static str,
    source: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    reason: Option<&'static str>,
}

#[derive(Debug, Serialize)]
struct ErrorResponse {
    ok: bool,
    error: String,
}

struct HttpRequest {
    method: String,
    path: String,
    body: Vec<u8>,
}

pub struct HttpWakeMonitor {
    event_rx: Receiver<SerialWakeEvent>,
    _task: Option<JoinHandle<()>>,
}

struct HttpWakeState {
    config: WakeConfig,
    last_emit_at: Mutex<Option<Instant>>,
}

impl HttpWakeMonitor {
    pub fn spawn(config: WakeConfig) -> Self {
        let (event_tx, event_rx) = unbounded();
        if !config.http_enabled {
            return Self {
                event_rx,
                _task: None,
            };
        }

        let task = tokio::spawn(async move {
            if let Err(err) = run_server(config, event_tx).await {
                error!(error = %err, "[WAKE][ERROR] HTTP 唤醒服务退出");
            }
        });
        Self {
            event_rx,
            _task: Some(task),
        }
    }

    pub fn try_recv(&self) -> Result<SerialWakeEvent, TryRecvError> {
        self.event_rx.try_recv()
    }
}

async fn run_server(config: WakeConfig, event_tx: Sender<SerialWakeEvent>) -> Result<()> {
    let addr = format!("{}:{}", config.http_host, config.http_port);
    let listener = TcpListener::bind(&addr).await?;
    info!(
        "[WAKE] HTTP 唤醒服务已启动: http://{}{}",
        addr, config.http_path
    );

    let state = Arc::new(HttpWakeState {
        config,
        last_emit_at: Mutex::new(None),
    });

    loop {
        let (stream, peer) = listener.accept().await?;
        let state = Arc::clone(&state);
        let event_tx = event_tx.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_connection(stream, state, event_tx).await {
                warn!(peer = %peer, error = %err, "[WAKE][WARN] HTTP 唤醒请求处理失败");
            }
        });
    }
}

async fn handle_connection(
    mut stream: TcpStream,
    state: Arc<HttpWakeState>,
    event_tx: Sender<SerialWakeEvent>,
) -> Result<()> {
    let request = read_http_request(&mut stream).await?;
    let path = request
        .path
        .split('?')
        .next()
        .unwrap_or(request.path.as_str());
    match (request.method.as_str(), path) {
        ("GET", path) | ("POST", path) if path == state.config.http_path => {
            let payload = match parse_payload(&request) {
                Ok(payload) => payload,
                Err(err) => {
                    write_json_response(
                        &mut stream,
                        400,
                        "Bad Request",
                        &ErrorResponse {
                            ok: false,
                            error: format!("invalid json: {err}"),
                        },
                    )
                    .await?;
                    return Ok(());
                }
            };
            let emitted = emit_wake_event(payload, &state, &event_tx);
            let response = if emitted {
                HttpWakeResponse {
                    ok: true,
                    status: "accepted",
                    source: HTTP_WAKE_SOURCE,
                    reason: None,
                }
            } else {
                HttpWakeResponse {
                    ok: true,
                    status: "ignored",
                    source: HTTP_WAKE_SOURCE,
                    reason: Some("debounced"),
                }
            };
            write_json_response(&mut stream, 200, "OK", &response).await?;
        }
        _ => {
            write_json_response(
                &mut stream,
                404,
                "Not Found",
                &ErrorResponse {
                    ok: false,
                    error: "not found".to_string(),
                },
            )
            .await?;
        }
    }
    Ok(())
}

fn parse_payload(request: &HttpRequest) -> Result<HttpWakePayload> {
    if request.method == "GET" || request.body.is_empty() {
        return Ok(HttpWakePayload {
            keyword: None,
            angle: None,
            beamforming: None,
            raw: None,
        });
    }
    serde_json::from_slice(&request.body).map_err(Into::into)
}

fn emit_wake_event(
    payload: HttpWakePayload,
    state: &HttpWakeState,
    event_tx: &Sender<SerialWakeEvent>,
) -> bool {
    let debounce = Duration::from_secs_f32(state.config.debounce_secs);
    let now = Instant::now();
    {
        let mut last_emit_at = state.last_emit_at.lock().unwrap();
        if let Some(last) = *last_emit_at {
            if now.duration_since(last) < debounce {
                return false;
            }
        }
        *last_emit_at = Some(now);
    }

    let event = SerialWakeEvent {
        port: HTTP_WAKE_SOURCE.to_string(),
        raw: payload.raw.unwrap_or_else(|| HTTP_WAKE_SOURCE.to_string()),
        fields: WakeFields {
            keyword: payload
                .keyword
                .or_else(|| Some(HTTP_WAKE_SOURCE.to_string())),
            angle: payload.angle,
            beamforming: payload.beamforming,
        },
        received_at: now,
    };
    let _ = event_tx.send(event);
    true
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
    let header = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        status,
        reason,
        body.len()
    );
    stream.write_all(header.as_bytes()).await?;
    stream.write_all(&body).await?;
    stream.shutdown().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn get_wakeup_uses_default_payload() {
        let request = HttpRequest {
            method: "GET".to_string(),
            path: "/wakeup".to_string(),
            body: Vec::new(),
        };
        let payload = parse_payload(&request).unwrap();
        assert!(payload.keyword.is_none());
        assert!(payload.angle.is_none());
    }

    #[test]
    fn post_wakeup_parses_optional_fields() {
        let request = HttpRequest {
            method: "POST".to_string(),
            path: "/wakeup".to_string(),
            body: br#"{"keyword":"hey","angle":"90","beamforming":"left","raw":"test"}"#.to_vec(),
        };
        let payload = parse_payload(&request).unwrap();
        assert_eq!(payload.keyword.as_deref(), Some("hey"));
        assert_eq!(payload.angle.as_deref(), Some("90"));
        assert_eq!(payload.beamforming.as_deref(), Some("left"));
        assert_eq!(payload.raw.as_deref(), Some("test"));
    }
}
