use crate::config::EnvironmentTriggerConfig;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::oneshot,
    task::JoinHandle,
    time,
};
use tracing::{error, info, warn};

#[derive(Debug)]
pub struct EnvironmentTalkRequest {
    pub content: String,
    pub received_at: Instant,
    respond_to: Option<oneshot::Sender<EnvironmentTalkResponse>>,
}

impl EnvironmentTalkRequest {
    /// 主动消费 request 并向 HTTP handler 发送响应。
    ///
    /// 接收 `self`（by value）确保调用一次后 request 不能再被使用，避免双发。
    /// 如果 request 因为 `?` 错误上抛、循环 `continue` 或其它原因被 drop，
    /// `Drop` impl 会自动发送 `ignored("dropped")` 占位响应。
    pub fn respond(mut self, response: EnvironmentTalkResponse) {
        if let Some(sender) = self.respond_to.take() {
            let _ = sender.send(response);
        }
    }

    /// 当前 HTTP 客户端是否已经断开（receiver 被 drop），
    /// 用于在主循环里跳过无效 send。
    pub fn is_closed(&self) -> bool {
        self.respond_to
            .as_ref()
            .map(|s| s.is_closed())
            .unwrap_or(true)
    }
}

impl Drop for EnvironmentTalkRequest {
    fn drop(&mut self) {
        if let Some(sender) = self.respond_to.take() {
            // 调用方忘了 respond()（比如 `?` 提前 return），自动发一个 dropped
            // 占位响应，防止 HTTP 处理器永远 hang 在 1s timeout 上。
            let _ = sender.send(EnvironmentTalkResponse::ignored("dropped"));
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct EnvironmentTalkResponse {
    pub ok: bool,
    pub status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<&'static str>,
}

impl EnvironmentTalkResponse {
    pub fn accepted() -> Self {
        Self {
            ok: true,
            status: "accepted",
            reason: None,
        }
    }

    pub fn ignored(reason: &'static str) -> Self {
        Self {
            ok: true,
            status: "ignored",
            reason: Some(reason),
        }
    }
}

#[derive(Debug, Deserialize)]
struct EnvironmentTalkPayload {
    content: Option<String>,
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

#[derive(Clone, Default)]
pub struct EnvironmentTriggerQueue {
    latest: Arc<Mutex<Option<EnvironmentTalkRequest>>>,
}

impl EnvironmentTriggerQueue {
    fn replace_latest(&self, request: EnvironmentTalkRequest) {
        let replaced = {
            let mut latest = self.latest.lock().unwrap();
            latest.replace(request)
        };
        if let Some(replaced) = replaced {
            replaced.respond(EnvironmentTalkResponse::ignored("replaced"));
        }
    }

    pub fn take_latest(&self) -> Option<EnvironmentTalkRequest> {
        self.latest.lock().unwrap().take()
    }
}

pub fn spawn_environment_trigger_server(
    config: EnvironmentTriggerConfig,
) -> (JoinHandle<()>, EnvironmentTriggerQueue) {
    let queue = EnvironmentTriggerQueue::default();
    let server_queue = queue.clone();
    let task = tokio::spawn(async move {
        if let Err(err) = run_server(config, server_queue).await {
            error!(error = %err, "[ENV][ERROR] 环境触发 HTTP 服务退出");
        }
    });
    (task, queue)
}

async fn run_server(
    config: EnvironmentTriggerConfig,
    queue: EnvironmentTriggerQueue,
) -> Result<()> {
    let addr = format!("{}:{}", config.host, config.port);
    let listener = TcpListener::bind(&addr).await?;
    info!(
        "[ENV] 环境触发 HTTP 服务已启动: http://{}{}",
        addr, config.path
    );

    loop {
        let (stream, peer) = listener.accept().await?;
        let config = config.clone();
        let queue = queue.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_connection(stream, config, queue).await {
                warn!(peer = %peer, error = %err, "[ENV][WARN] 环境触发请求处理失败");
            }
        });
    }
}

async fn handle_connection(
    mut stream: TcpStream,
    config: EnvironmentTriggerConfig,
    queue: EnvironmentTriggerQueue,
) -> Result<()> {
    let request = read_http_request(&mut stream).await?;
    match (request.method.as_str(), request.path.as_str()) {
        ("POST", path) if path == config.path => {
            let payload: EnvironmentTalkPayload = match serde_json::from_slice(&request.body) {
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

            let content = payload.content.unwrap_or_default().trim().to_string();
            if content.is_empty() {
                write_json_response(
                    &mut stream,
                    400,
                    "Bad Request",
                    &ErrorResponse {
                        ok: false,
                        error: "content is required".to_string(),
                    },
                )
                .await?;
                return Ok(());
            }

            if content.chars().count() > config.max_content_chars {
                write_json_response(
                    &mut stream,
                    400,
                    "Bad Request",
                    &ErrorResponse {
                        ok: false,
                        error: format!(
                            "content is too long, max {} chars",
                            config.max_content_chars
                        ),
                    },
                )
                .await?;
                return Ok(());
            }

            let (respond_to, response_rx) = oneshot::channel();
            let request = EnvironmentTalkRequest {
                content,
                received_at: Instant::now(),
                respond_to: Some(respond_to),
            };

            queue.replace_latest(request);

            let response = match time::timeout(Duration::from_secs(1), response_rx).await {
                Ok(Ok(response)) => response,
                Ok(Err(_)) | Err(_) => EnvironmentTalkResponse::ignored("gateway_disconnected"),
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
