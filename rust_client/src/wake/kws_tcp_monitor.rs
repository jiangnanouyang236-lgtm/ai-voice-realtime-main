use crate::config::WakeConfig;
use crate::wake::serial_monitor::{SerialWakeEvent, WakeFields};
use anyhow::Result;
use crossbeam::channel::{bounded, Receiver, Sender, TryRecvError, TrySendError};
use serde::Deserialize;
use std::{
    io::{self, BufRead, BufReader},
    net::{TcpStream, ToSocketAddrs},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tracing::{info, warn};

const KWS_WAKE_SOURCE: &str = "kws_tcp";
const KWS_EVENT_CAPACITY: usize = 16;
const KWS_CONNECT_TIMEOUT: Duration = Duration::from_millis(500);
const KWS_READ_TIMEOUT: Duration = Duration::from_millis(200);
const KWS_RECONNECT_POLL_INTERVAL: Duration = Duration::from_millis(50);
const KWS_JSONL_MAX_BYTES: usize = 4096;

#[derive(Debug, Deserialize)]
struct KwsWakePayload {
    #[serde(rename = "type")]
    event_type: String,
    seq: Option<u64>,
    frame_index: Option<u64>,
    keyword: Option<String>,
    score: Option<f32>,
    source: Option<String>,
    detected_epoch_us: Option<u64>,
    published_epoch_us: Option<u64>,
    publish_delay_us: Option<u64>,
    #[serde(default)]
    emits_wake: Option<bool>,
    #[allow(dead_code)]
    interrupt_allowed: Option<bool>,
}

pub struct KwsWakeMonitor {
    event_rx: Receiver<SerialWakeEvent>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl KwsWakeMonitor {
    pub fn spawn(config: WakeConfig) -> Self {
        let (event_tx, event_rx) = bounded(KWS_EVENT_CAPACITY);
        let stop = Arc::new(AtomicBool::new(false));
        if !config.source_mode.kws_enabled() {
            return Self {
                event_rx,
                stop,
                thread: None,
            };
        }

        let stop_for_thread = Arc::clone(&stop);
        let thread = thread::spawn(move || run_worker(config, event_tx, stop_for_thread));
        Self {
            event_rx,
            stop,
            thread: Some(thread),
        }
    }

    pub fn try_recv(&self) -> Result<SerialWakeEvent, TryRecvError> {
        self.event_rx.try_recv()
    }
}

impl Drop for KwsWakeMonitor {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            thread.thread().unpark();
            let _ = thread.join();
        }
    }
}

fn run_worker(config: WakeConfig, event_tx: Sender<SerialWakeEvent>, stop: Arc<AtomicBool>) {
    let address = format!("{}:{}", config.kws_host, config.kws_port);
    let reconnect_delay = Duration::from_secs_f32(config.kws_reconnect_secs);

    while !stop.load(Ordering::Acquire) {
        match connect_with_timeout(&config.kws_host, config.kws_port, KWS_CONNECT_TIMEOUT) {
            Ok(stream) => {
                info!("[WAKE] KWS TCP 唤醒事件已连接: {}", address);
                match read_events(stream, &event_tx, &stop) {
                    Ok(()) => break,
                    Err(err) if !stop.load(Ordering::Acquire) => {
                        warn!("[WAKE][WARN] KWS TCP 唤醒事件读取失败 {}: {}", address, err);
                    }
                    Err(_) => break,
                }
            }
            Err(err) if !stop.load(Ordering::Acquire) => {
                warn!("[WAKE][WARN] KWS TCP 唤醒事件连接失败 {}: {}", address, err);
            }
            Err(_) => break,
        }
        if wait_for_stop(&stop, reconnect_delay) {
            break;
        }
    }
}

fn connect_with_timeout(host: &str, port: u16, timeout: Duration) -> io::Result<TcpStream> {
    let mut last_error = None;
    let mut resolved = false;
    for address in (host, port).to_socket_addrs()? {
        resolved = true;
        match TcpStream::connect_timeout(&address, timeout) {
            Ok(stream) => return Ok(stream),
            Err(err) => last_error = Some(err),
        }
    }
    Err(last_error.unwrap_or_else(|| {
        io::Error::new(
            io::ErrorKind::AddrNotAvailable,
            if resolved {
                "KWS TCP 没有可连接的地址"
            } else {
                "KWS TCP 地址解析结果为空"
            },
        )
    }))
}

fn wait_for_stop(stop: &AtomicBool, duration: Duration) -> bool {
    let deadline = Instant::now() + duration;
    loop {
        if stop.load(Ordering::Acquire) {
            return true;
        }
        let now = Instant::now();
        if now >= deadline {
            return false;
        }
        thread::park_timeout((deadline - now).min(KWS_RECONNECT_POLL_INTERVAL));
    }
}

fn read_events(
    stream: TcpStream,
    event_tx: &Sender<SerialWakeEvent>,
    stop: &AtomicBool,
) -> Result<()> {
    stream.set_read_timeout(Some(KWS_READ_TIMEOUT))?;
    let mut reader = BufReader::new(stream);

    loop {
        let bytes = match read_bounded_line(&mut reader, stop)? {
            BoundedLine::Line(bytes) => bytes,
            BoundedLine::Oversized => {
                warn!(
                    max_bytes = KWS_JSONL_MAX_BYTES,
                    "[WAKE][WARN] KWS TCP 唤醒事件超过长度上限，已丢弃"
                );
                continue;
            }
            BoundedLine::Closed => anyhow::bail!("connection closed"),
            BoundedLine::Cancelled => return Ok(()),
        };
        let line = match std::str::from_utf8(&bytes) {
            Ok(line) => line,
            Err(err) => {
                warn!("[WAKE][WARN] KWS TCP 唤醒事件不是 UTF-8: {}", err);
                continue;
            }
        };
        match parse_kws_wake_event(line) {
            Ok(Some(event)) => match event_tx.try_send(event) {
                Ok(()) => {}
                Err(TrySendError::Full(_)) => {
                    warn!(
                        capacity = KWS_EVENT_CAPACITY,
                        "[WAKE][WARN] KWS TCP 唤醒事件队列已满，已丢弃新事件"
                    );
                }
                Err(TrySendError::Disconnected(_)) => return Ok(()),
            },
            Ok(None) => {}
            Err(err) => {
                warn!("[WAKE][WARN] KWS TCP 唤醒事件解析失败: {}", err);
            }
        }
    }
}

enum BoundedLine {
    Line(Vec<u8>),
    Oversized,
    Closed,
    Cancelled,
}

fn read_bounded_line<R: BufRead>(reader: &mut R, stop: &AtomicBool) -> io::Result<BoundedLine> {
    let mut line = Vec::new();
    let mut oversized = false;

    loop {
        if stop.load(Ordering::Acquire) {
            return Ok(BoundedLine::Cancelled);
        }

        let buffer = match reader.fill_buf() {
            Ok(buffer) => buffer,
            Err(err)
                if matches!(
                    err.kind(),
                    io::ErrorKind::WouldBlock
                        | io::ErrorKind::TimedOut
                        | io::ErrorKind::Interrupted
                ) =>
            {
                continue;
            }
            Err(err) => return Err(err),
        };

        if buffer.is_empty() {
            if oversized {
                return Ok(BoundedLine::Oversized);
            }
            if line.is_empty() {
                return Ok(BoundedLine::Closed);
            }
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            return Ok(BoundedLine::Line(line));
        }

        let newline_index = buffer.iter().position(|byte| *byte == b'\n');
        let segment_len = newline_index.unwrap_or(buffer.len());
        if !oversized {
            if line.len().saturating_add(segment_len) <= KWS_JSONL_MAX_BYTES {
                line.extend_from_slice(&buffer[..segment_len]);
            } else {
                oversized = true;
                line.clear();
            }
        }
        reader.consume(segment_len + usize::from(newline_index.is_some()));

        if newline_index.is_some() {
            if oversized {
                return Ok(BoundedLine::Oversized);
            }
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            return Ok(BoundedLine::Line(line));
        }
    }
}

fn parse_kws_wake_event(line: &str) -> Result<Option<SerialWakeEvent>> {
    let raw = line.trim();
    if raw.is_empty() {
        return Ok(None);
    }

    let payload: KwsWakePayload = serde_json::from_str(raw)?;
    if payload.event_type != "kws_detected" {
        return Ok(None);
    }
    if payload.emits_wake == Some(false) {
        return Ok(None);
    }

    let Some(keyword) = payload.keyword.map(|value| value.trim().to_string()) else {
        return Ok(None);
    };
    if keyword.is_empty() {
        return Ok(None);
    }

    info!(
        seq = payload.seq,
        frame_index = payload.frame_index,
        score = payload.score,
        source = payload.source.as_deref().unwrap_or("-"),
        detected_epoch_us = payload.detected_epoch_us,
        published_epoch_us = payload.published_epoch_us,
        publish_delay_us = payload.publish_delay_us,
        keyword = %keyword,
        "[WAKE] 收到 KWS TCP 唤醒事件"
    );

    Ok(Some(SerialWakeEvent {
        port: KWS_WAKE_SOURCE.to_string(),
        raw: raw.to_string(),
        fields: WakeFields {
            keyword: Some(keyword),
            angle: None,
            beamforming: None,
        },
        received_at: Instant::now(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::WakeSourceMode;
    use std::{
        io::{Read, Write},
        net::TcpListener,
        time::{Duration, Instant},
    };

    fn test_wake_config(port: u16) -> WakeConfig {
        WakeConfig {
            source_mode: WakeSourceMode::Kws,
            serial_port: None,
            serial_baud: 115_200,
            serial_timeout_secs: 0.2,
            serial_reconnect_secs: 1.0,
            debounce_secs: 0.5,
            http_enabled: false,
            http_host: "127.0.0.1".to_string(),
            http_port: 5_202,
            http_path: "/wakeup".to_string(),
            kws_host: "127.0.0.1".to_string(),
            kws_port: port,
            kws_reconnect_secs: 0.1,
            angle_report_url: "http://127.0.0.1:19992/api/robot/angle".to_string(),
            angle_report_timeout_secs: 1.0,
        }
    }

    #[test]
    fn parses_kws_detected_jsonl_as_wake_event() {
        let event = parse_kws_wake_event(
            r#"{"type":"kws_detected","keyword":"你好康康","score":1,"emits_wake":true}"#,
        )
        .expect("json should parse")
        .expect("event should emit");

        assert_eq!(event.port, "kws_tcp");
        assert_eq!(event.fields.keyword.as_deref(), Some("你好康康"));
        assert!(event.raw.contains(r#""type":"kws_detected""#));
    }

    #[test]
    fn ignores_non_wake_or_disabled_events() {
        assert!(
            parse_kws_wake_event(r#"{"type":"ping","keyword":"你好康康"}"#)
                .unwrap()
                .is_none()
        );
        assert!(parse_kws_wake_event(
            r#"{"type":"kws_detected","keyword":"你好康康","emits_wake":false}"#
        )
        .unwrap()
        .is_none());
        assert!(
            parse_kws_wake_event(r#"{"type":"kws_detected","keyword":" "}"#)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn monitor_receives_jsonl_from_tcp_server() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().unwrap().port();
        let monitor = KwsWakeMonitor::spawn(test_wake_config(port));

        thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept test client");
            writeln!(
                stream,
                r#"{{"type":"kws_detected","keyword":"你好康康","score":1}}"#
            )
            .expect("write event");
        });

        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if let Ok(event) = monitor.try_recv() {
                assert_eq!(event.port, "kws_tcp");
                assert_eq!(event.fields.keyword.as_deref(), Some("你好康康"));
                return;
            }
            assert!(Instant::now() < deadline, "timed out waiting for KWS event");
            thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn oversized_line_is_discarded_and_next_event_is_received() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("local addr").port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept test client");
            stream
                .write_all(&vec![b'x'; KWS_JSONL_MAX_BYTES + 128])
                .expect("write oversized line");
            stream.write_all(b"\n").expect("finish oversized line");
            writeln!(
                stream,
                r#"{{"type":"kws_detected","keyword":"你好康康","score":1}}"#
            )
            .expect("write valid event");
        });

        let stream = TcpStream::connect(("127.0.0.1", port)).expect("connect test client");
        let (event_tx, event_rx) = bounded(KWS_EVENT_CAPACITY);
        let stop = AtomicBool::new(false);
        let result = read_events(stream, &event_tx, &stop);
        assert!(result.is_err(), "server close should end the read loop");
        server.join().expect("server thread");

        let event = event_rx
            .try_recv()
            .expect("valid event after oversized line");
        assert_eq!(event.fields.keyword.as_deref(), Some("你好康康"));
        assert!(event_rx.try_recv().is_err());
    }

    #[test]
    fn dropping_monitor_stops_and_joins_worker() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("local addr").port();
        let (accepted_tx, accepted_rx) = bounded(1);
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept test client");
            accepted_tx.send(()).expect("signal accepted client");
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .expect("set server timeout");
            let mut byte = [0u8; 1];
            match stream.read(&mut byte) {
                Ok(0) => {}
                Err(err)
                    if matches!(
                        err.kind(),
                        io::ErrorKind::ConnectionReset | io::ErrorKind::BrokenPipe
                    ) => {}
                other => panic!("client should close after monitor drop, got {other:?}"),
            }
        });

        let monitor = KwsWakeMonitor::spawn(test_wake_config(port));
        accepted_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("worker should connect");

        let started_at = Instant::now();
        drop(monitor);
        assert!(
            started_at.elapsed() < Duration::from_secs(1),
            "monitor drop should interrupt the read loop promptly"
        );
        server.join().expect("server thread");
    }
}
