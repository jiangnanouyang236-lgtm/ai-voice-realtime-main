use crate::config::WakeConfig;
use crossbeam::channel::{unbounded, Receiver, TryRecvError};
use serde_json::json;
use serde_json::Value;
use serialport::{available_ports, SerialPortType};
use std::{
    collections::HashSet,
    io::{ErrorKind, Read},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};
use tracing::{info, warn};

const WAKE_SERIAL_PATTERNS: [&str; 5] =
    ["usbserial", "usbmodem", "ttyacm", "ttyusb", "wchusbserial"];

#[derive(Debug, Clone, Default)]
pub struct WakeFields {
    pub keyword: Option<String>,
    pub angle: Option<String>,
    pub beamforming: Option<String>,
}

#[derive(Debug, Clone)]
pub struct SerialWakeEvent {
    pub port: String,
    pub raw: String,
    pub fields: WakeFields,
    pub received_at: Instant,
}

pub struct SerialWakeMonitor {
    event_rx: Receiver<SerialWakeEvent>,
    _state: Arc<MonitorState>,
}

struct MonitorState {
    config: WakeConfig,
    active_ports: Mutex<HashSet<String>>,
    last_emit_at: Mutex<Option<Instant>>,
}

impl SerialWakeMonitor {
    pub fn new(config: WakeConfig) -> Self {
        let (event_tx, event_rx) = unbounded();
        let state = Arc::new(MonitorState {
            config,
            active_ports: Mutex::new(HashSet::new()),
            last_emit_at: Mutex::new(None),
        });

        let state_for_supervisor = Arc::clone(&state);
        thread::spawn(move || loop {
            let reconnect_delay =
                Duration::from_secs_f32(state_for_supervisor.config.serial_reconnect_secs);
            let ports = find_wake_serial_ports(state_for_supervisor.config.serial_port.as_deref());
            if let Some(port) = ports.into_iter().next() {
                let should_spawn = {
                    let mut active = state_for_supervisor.active_ports.lock().unwrap();
                    if active.contains(&port) {
                        false
                    } else {
                        active.insert(port.clone());
                        true
                    }
                };
                if should_spawn {
                    let state_for_reader = Arc::clone(&state_for_supervisor);
                    let event_tx_for_reader = event_tx.clone();
                    thread::spawn(move || {
                        reader_worker(port, state_for_reader, event_tx_for_reader);
                    });
                }
            }
            thread::sleep(reconnect_delay);
        });

        Self {
            event_rx,
            _state: state,
        }
    }

    pub fn try_recv(&self) -> Result<SerialWakeEvent, TryRecvError> {
        self.event_rx.try_recv()
    }
}

fn reader_worker(
    port: String,
    state: Arc<MonitorState>,
    event_tx: crossbeam::channel::Sender<SerialWakeEvent>,
) {
    let timeout = Duration::from_secs_f32(state.config.serial_timeout_secs);
    let result = serialport::new(&port, state.config.serial_baud)
        .timeout(timeout)
        .open();

    let mut serial = match result {
        Ok(serial) => serial,
        Err(err) => {
            warn!("[WAKE][WARN] 唤醒串口打开失败 {}: {}", port, err);
            state.active_ports.lock().unwrap().remove(&port);
            return;
        }
    };

    info!("[WAKE] 硬件唤醒串口已连接: {}", port);

    let debounce = Duration::from_secs_f32(state.config.debounce_secs);
    let mut read_buf = [0u8; 1024];
    let mut pending = Vec::new();

    loop {
        match serial.read(&mut read_buf) {
            Ok(0) => continue,
            Ok(n) => {
                pending.extend_from_slice(&read_buf[..n]);

                while let Some(newline_pos) = pending.iter().position(|&byte| byte == b'\n') {
                    let raw_line: Vec<u8> = pending.drain(..=newline_pos).collect();
                    if let Some(event) = parse_wake_event(&port, &raw_line, &state, debounce) {
                        let _ = event_tx.send(event);
                    }
                }
            }
            Err(err) if err.kind() == ErrorKind::TimedOut => {
                if !pending.is_empty() {
                    let raw_line = std::mem::take(&mut pending);
                    if let Some(event) = parse_wake_event(&port, &raw_line, &state, debounce) {
                        let _ = event_tx.send(event);
                    }
                }
                continue;
            }
            Err(err) => {
                warn!("[WAKE][WARN] 唤醒串口读取失败 {}: {}", port, err);
                break;
            }
        }
    }

    warn!("[WAKE][WARN] 唤醒串口已断开: {}", port);
    state.active_ports.lock().unwrap().remove(&port);
}

fn parse_wake_event(
    port: &str,
    raw_line: &[u8],
    state: &MonitorState,
    debounce: Duration,
) -> Option<SerialWakeEvent> {
    let text = String::from_utf8_lossy(raw_line).trim().to_string();
    if text.is_empty() {
        return None;
    }

    let parsed = parse_json_value(&text);
    let fields = parsed
        .as_ref()
        .map(extract_wake_fields)
        .unwrap_or_else(|| fallback_extract_wake_fields_from_text(&text));

    if !has_wake_fields(&fields) {
        return None;
    }

    report_angle_async(
        state.config.angle_report_url.clone(),
        state.config.angle_report_timeout_secs,
        port.to_string(),
        fields.angle.clone(),
    );

    let now = Instant::now();
    {
        let mut last_emit_at = state.last_emit_at.lock().unwrap();
        if let Some(last) = *last_emit_at {
            if now.duration_since(last) < debounce {
                return None;
            }
        }
        *last_emit_at = Some(now);
    }

    Some(SerialWakeEvent {
        port: port.to_string(),
        raw: text,
        fields,
        received_at: now,
    })
}

fn has_wake_fields(fields: &WakeFields) -> bool {
    fields
        .keyword
        .as_deref()
        .is_some_and(|value| !value.trim().is_empty())
        || fields
            .angle
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty())
        || fields
            .beamforming
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty())
}

fn parse_json_value(text: &str) -> Option<Value> {
    serde_json::from_str::<Value>(text).ok().or_else(|| {
        extract_first_json_object(text)
            .and_then(|candidate| serde_json::from_str::<Value>(&candidate).ok())
    })
}

fn report_angle_async(url: String, timeout_secs: f32, port: String, angle: Option<String>) {
    let Some(angle) = angle else {
        return;
    };

    thread::spawn(move || {
        let timeout = Duration::from_secs_f32(timeout_secs.max(0.1));
        let client = match reqwest::blocking::Client::builder()
            .timeout(timeout)
            .build()
        {
            Ok(client) => client,
            Err(err) => {
                warn!(
                    "[ANGLE][WARN] 角度上报客户端初始化失败 {} {}: {}",
                    port, angle, err
                );
                return;
            }
        };

        match client.post(&url).json(&json!({ "angle": angle })).send() {
            Ok(response) => {
                info!(
                    "[ANGLE] 角度上报成功: port={}, angle={:?}, status={}",
                    port,
                    angle,
                    response.status()
                );
            }
            Err(err) => {
                warn!("[ANGLE][WARN] 角度上报失败 {} {:?}: {}", port, angle, err);
            }
        }
    });
}

fn extract_first_json_object(text: &str) -> Option<String> {
    let start = text.find('{')?;
    let mut depth = 0usize;
    let mut in_string = false;
    let mut escape = false;

    for (offset, ch) in text[start..].char_indices() {
        if in_string {
            if escape {
                escape = false;
                continue;
            }
            match ch {
                '\\' => escape = true,
                '"' => in_string = false,
                _ => {}
            }
            continue;
        }

        match ch {
            '"' => in_string = true,
            '{' => depth += 1,
            '}' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    let end = start + offset + ch.len_utf8();
                    return Some(text[start..end].to_string());
                }
            }
            _ => {}
        }
    }

    None
}

fn extract_wake_fields(value: &Value) -> WakeFields {
    let legacy = extract_legacy_wake_fields(value);
    let aiui = extract_aiui_wake_fields(value);

    WakeFields {
        keyword: legacy.keyword.or(aiui.keyword),
        angle: legacy.angle.or(aiui.angle),
        beamforming: legacy.beamforming.or(aiui.beamforming),
    }
}

fn extract_legacy_wake_fields(value: &Value) -> WakeFields {
    let param1 = value.get("param1").and_then(Value::as_object);
    let keyword = param1
        .and_then(|obj| obj.get("keyword"))
        .and_then(value_to_string);
    let angle = value
        .get("ivw_cbf angle")
        .and_then(value_to_string)
        .or_else(|| {
            param1
                .and_then(|obj| obj.get("angle"))
                .and_then(value_to_string)
        });
    let beamforming = value.get("beamforming").and_then(value_to_string);

    WakeFields {
        keyword,
        angle,
        beamforming,
    }
}

fn extract_aiui_wake_fields(value: &Value) -> WakeFields {
    let Some(content) = value.get("content").and_then(Value::as_object) else {
        return WakeFields::default();
    };

    let info = content.get("info").and_then(|value| match value {
        Value::String(text) => serde_json::from_str::<Value>(text).ok(),
        Value::Object(_) => Some(value.clone()),
        _ => None,
    });
    let ivw = info
        .as_ref()
        .and_then(|value| value.get("ivw"))
        .and_then(Value::as_object);

    WakeFields {
        keyword: ivw
            .and_then(|obj| obj.get("keyword"))
            .and_then(value_to_string)
            .or_else(|| content.get("result").and_then(value_to_string)),
        angle: ivw
            .and_then(|obj| obj.get("angle"))
            .and_then(value_to_string),
        beamforming: ivw
            .and_then(|obj| obj.get("beam"))
            .and_then(value_to_string),
    }
}

fn value_to_string(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(flag) => Some(flag.to_string()),
        _ => None,
    }
}

fn fallback_extract_wake_fields_from_text(text: &str) -> WakeFields {
    WakeFields {
        keyword: extract_json_string_field(text, "keyword"),
        angle: extract_json_string_field(text, "ivw_cbf angle")
            .or_else(|| extract_json_number_field(text, "angle")),
        beamforming: extract_json_string_field(text, "beamforming"),
    }
}

fn extract_json_string_field(text: &str, key: &str) -> Option<String> {
    let marker = format!("\"{key}\"");
    let start = text.find(&marker)? + marker.len();
    let rest = text[start..].trim_start();
    let rest = rest.strip_prefix(':')?.trim_start();
    let rest = rest.strip_prefix('"')?;
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

fn extract_json_number_field(text: &str, key: &str) -> Option<String> {
    let marker = format!("\"{key}\"");
    let start = text.find(&marker)? + marker.len();
    let rest = text[start..].trim_start();
    let rest = rest.strip_prefix(':')?.trim_start();
    let number: String = rest
        .chars()
        .take_while(|ch| ch.is_ascii_digit() || matches!(ch, '.' | '-'))
        .collect();
    if number.is_empty() {
        None
    } else {
        Some(number)
    }
}

fn serial_port_identity(port: &str) -> String {
    let base = port.rsplit('/').next().unwrap_or(port).to_ascii_lowercase();
    if let Some(stripped) = base.strip_prefix("cu.") {
        stripped.to_string()
    } else if let Some(stripped) = base.strip_prefix("tty.") {
        stripped.to_string()
    } else {
        base
    }
}

fn serial_port_rank(port: &str) -> usize {
    let lower = port.to_ascii_lowercase();
    if lower.contains("/dev/cu.") {
        0
    } else if lower.contains("/dev/tty.") {
        1
    } else {
        0
    }
}

fn dedupe_serial_candidates(candidates: Vec<String>) -> Vec<String> {
    let mut ordered: Vec<(usize, usize, String)> = candidates
        .into_iter()
        .enumerate()
        .map(|(idx, port)| (serial_port_rank(&port), idx, port))
        .collect();
    ordered.sort_by_key(|(rank, idx, _)| (*rank, *idx));

    let mut deduped = Vec::new();
    let mut seen = HashSet::new();
    for (_, _, port) in ordered {
        let identity = serial_port_identity(&port);
        if seen.insert(identity) {
            deduped.push(port);
        }
    }
    deduped
}

fn find_wake_serial_ports(explicit_port: Option<&str>) -> Vec<String> {
    if let Some(port) = explicit_port {
        return vec![port.to_string()];
    }

    let mut preferred = Vec::new();
    let mut fallback = Vec::new();

    if let Ok(ports) = available_ports() {
        for port in ports {
            let device_lower = port.port_name.to_lowercase();
            let mut matched_preferred = false;

            if let SerialPortType::UsbPort(usb) = &port.port_type {
                if usb.vid == 0x303A {
                    preferred.push(port.port_name.clone());
                    matched_preferred = true;
                }
            }

            if matched_preferred {
                continue;
            }

            let meta = match &port.port_type {
                SerialPortType::UsbPort(usb) => format!(
                    "{} {} {:?}",
                    usb.product.as_deref().unwrap_or_default().to_lowercase(),
                    usb.manufacturer
                        .as_deref()
                        .unwrap_or_default()
                        .to_lowercase(),
                    usb.serial_number.as_deref()
                ),
                other => format!("{other:?}").to_lowercase(),
            };

            if WAKE_SERIAL_PATTERNS
                .iter()
                .any(|pattern| device_lower.contains(pattern) || meta.contains(pattern))
            {
                fallback.push(port.port_name.clone());
            }
        }
    }

    let merged = preferred.into_iter().chain(fallback).collect::<Vec<_>>();
    dedupe_serial_candidates(merged)
}

#[cfg(test)]
mod tests {
    use super::{extract_wake_fields, has_wake_fields, parse_json_value};
    use serde_json::json;

    #[test]
    fn extracts_legacy_wake_fields() {
        let value = json!({
            "param1": {
                "keyword": "xiao3 fei1 xiao3 fei1",
                "angle": 90
            },
            "beamforming": "left"
        });

        let fields = extract_wake_fields(&value);

        assert_eq!(fields.keyword.as_deref(), Some("xiao3 fei1 xiao3 fei1"));
        assert_eq!(fields.angle.as_deref(), Some("90"));
        assert_eq!(fields.beamforming.as_deref(), Some("left"));
    }

    #[test]
    fn extracts_aiui_wake_fields_from_embedded_info_json() {
        let raw = concat!(
            "\u{fffd}\u{1}\u{1}\u{4}\0prefix",
            r#"{"content":{"arg1":0,"arg2":0,"eventType":4,"info":"{\"ivw\":{\"start_ms\":54010,\"end_ms\":54900,\"beam\":0,\"physical\":0,\"score\":1680.0,\"power\":15183.162109375,\"angle\":3.0,\"keyword\":\"xiao3 fei1 xiao3 fei1\"}}","result":"xiao3 fei1 xiao3 fei1"},"type":"aiui_event"}"#,
            "P"
        );
        let value = parse_json_value(raw).expect("AIUI outer JSON should parse");

        let fields = extract_wake_fields(&value);

        assert_eq!(fields.keyword.as_deref(), Some("xiao3 fei1 xiao3 fei1"));
        assert_eq!(fields.angle.as_deref(), Some("3.0"));
        assert_eq!(fields.beamforming.as_deref(), Some("0"));
    }

    #[test]
    fn accepts_aiui_info_as_json_object_and_falls_back_to_result() {
        let value = json!({
            "content": {
                "info": {
                    "ivw": {
                        "angle": 12,
                        "beam": 1
                    }
                },
                "result": "wake word"
            },
            "type": "aiui_event"
        });

        let fields = extract_wake_fields(&value);

        assert_eq!(fields.keyword.as_deref(), Some("wake word"));
        assert_eq!(fields.angle.as_deref(), Some("12"));
        assert_eq!(fields.beamforming.as_deref(), Some("1"));
    }

    #[test]
    fn generic_serial_json_is_not_a_wake_signal() {
        let value = json!({
            "type": "status",
            "content": {"connected": true}
        });

        let fields = extract_wake_fields(&value);

        assert!(!has_wake_fields(&fields));
    }

    #[test]
    fn angle_only_legacy_payload_remains_a_wake_signal() {
        let value = json!({
            "ivw_cbf angle": 289.0,
            "beamforming": "0"
        });

        let fields = extract_wake_fields(&value);

        assert!(has_wake_fields(&fields));
    }
}
