use crate::{
    audio_frontend::protocol::{WIRE_CHANNELS, WIRE_FRAME_MS, WIRE_SAMPLE_RATE},
    audio_frontend::tcp::{connect_with_timeout, TCP_CONNECT_TIMEOUT},
    config::AudioConfig,
};
use anyhow::{Context, Result};
use serde::Deserialize;
use std::{
    io::{self, Read, Write},
    net::TcpStream,
    time::{Duration, Instant},
};

const CONTROL_WRITE_TIMEOUT: Duration = Duration::from_millis(200);
const CONTROL_READ_TIMEOUT: Duration = Duration::from_millis(300);
const CONTROL_RESPONSE_MAX_BYTES: usize = 64 * 1024;
const CONTROL_READ_CHUNK_BYTES: usize = 4096;

#[derive(Debug, Deserialize)]
pub struct AudioFrontendStatus {
    #[serde(rename = "type")]
    pub message_type: String,
    pub ok: Option<bool>,
    pub sample_rate: Option<u32>,
    pub channels: Option<u16>,
    pub frame_ms: Option<u16>,
    pub processor_backend: Option<String>,
    pub processor_aec_active: Option<bool>,
    pub processor_high_pass_filter_active: Option<bool>,
    pub processor_noise_suppression_active: Option<bool>,
    pub processor_agc_active: Option<bool>,
    pub processor_kws_audio_tap_active: Option<bool>,
    pub processor_webrtc_compiled: Option<bool>,
    pub processor_frames: Option<u64>,
    pub processor_missing_render: Option<u64>,
    pub processor_kws_frames: Option<u64>,
    pub processor_kws_errors: Option<u64>,
    pub processor_errors: Option<u64>,
    pub protocol_errors: Option<u64>,
    pub socket_errors: Option<u64>,
    pub error: Option<String>,
}

pub struct AudioFrontendControl {
    stream: TcpStream,
    read_buffer: Vec<u8>,
    seq: u64,
}

impl AudioFrontendControl {
    pub fn connect(audio: &AudioConfig) -> Result<Self> {
        let address = format!("{}:{}", audio.frontend_host, audio.frontend_control_port);
        let stream = connect_with_timeout(&address, TCP_CONNECT_TIMEOUT)
            .with_context(|| format!("连接 audio_frontend control TCP 失败: {address}"))?;
        stream
            .set_nodelay(true)
            .context("设置 audio_frontend control TCP_NODELAY 失败")?;
        stream
            .set_write_timeout(Some(CONTROL_WRITE_TIMEOUT))
            .context("设置 audio_frontend control write timeout 失败")?;
        stream
            .set_read_timeout(Some(CONTROL_READ_TIMEOUT))
            .context("设置 audio_frontend control read timeout 失败")?;
        tracing::info!(address = %address, "[AUDIO_FRONTEND] control TCP connected");
        Ok(Self {
            stream,
            read_buffer: Vec::new(),
            seq: 0,
        })
    }

    pub fn set_mic_output_muted(&mut self, enabled: bool) -> Result<()> {
        let seq = self.next_seq();
        self.send_line(&format!(
            r#"{{"type":"mute_mic_output","enabled":{enabled},"seq":{seq}}}"#
        ))
    }

    pub fn speaking_started(&mut self) -> Result<()> {
        let seq = self.next_seq();
        self.send_line(&format!(
            r#"{{"type":"speaking_started","seq":{},"timestamp_us":{}}}"#,
            seq,
            monotonic_us()
        ))
    }

    pub fn speaking_finished(&mut self) -> Result<()> {
        let seq = self.next_seq();
        self.send_line(&format!(
            r#"{{"type":"speaking_finished","seq":{},"timestamp_us":{}}}"#,
            seq,
            monotonic_us()
        ))
    }

    pub fn playback_interrupt(&mut self) -> Result<()> {
        let seq = self.next_seq();
        self.send_line(&format!(
            r#"{{"type":"playback_interrupt","seq":{},"timestamp_us":{}}}"#,
            seq,
            monotonic_us()
        ))
    }

    pub fn ping(&mut self) -> Result<()> {
        let seq = self.next_seq();
        self.send_line(&format!(
            r#"{{"type":"ping","seq":{seq},"timestamp_us":{}}}"#,
            monotonic_us()
        ))
    }

    pub fn restore_speaking_state(
        &mut self,
        speaking: bool,
        mute_mic_during_playback: bool,
    ) -> Result<()> {
        if speaking {
            if mute_mic_during_playback {
                self.set_mic_output_muted(true)?;
            }
            self.speaking_started()
        } else if mute_mic_during_playback {
            self.set_mic_output_muted(false)
        } else {
            Ok(())
        }
    }

    pub fn query_status(&mut self) -> Result<AudioFrontendStatus> {
        let seq = self.next_seq();
        self.send_line(&format!(
            r#"{{"type":"status","seq":{},"timestamp_us":{}}}"#,
            seq,
            monotonic_us()
        ))?;
        let line = self
            .read_line()
            .context("读取 audio_frontend status 响应失败")?;
        let status: AudioFrontendStatus =
            serde_json::from_str(&line).context("解析 audio_frontend status 响应失败")?;
        anyhow::ensure!(
            status.message_type == "status",
            "audio_frontend status 响应类型错误: {}",
            status.message_type
        );
        anyhow::ensure!(
            status.ok.unwrap_or(false),
            "audio_frontend status 不可用: {}",
            status.error.as_deref().unwrap_or("unknown")
        );
        anyhow::ensure!(
            status.sample_rate == Some(WIRE_SAMPLE_RATE),
            "audio_frontend status sample_rate 不匹配: expected {}, got {:?}",
            WIRE_SAMPLE_RATE,
            status.sample_rate
        );
        anyhow::ensure!(
            status.channels == Some(WIRE_CHANNELS),
            "audio_frontend status channels 不匹配: expected {}, got {:?}",
            WIRE_CHANNELS,
            status.channels
        );
        anyhow::ensure!(
            status.frame_ms == Some(WIRE_FRAME_MS),
            "audio_frontend status frame_ms 不匹配: expected {}, got {:?}",
            WIRE_FRAME_MS,
            status.frame_ms
        );
        Ok(status)
    }

    fn next_seq(&mut self) -> u64 {
        let seq = self.seq;
        self.seq = self.seq.wrapping_add(1);
        seq
    }

    fn send_line(&mut self, line: &str) -> Result<()> {
        self.stream
            .write_all(line.as_bytes())
            .context("写入 audio_frontend control 消息失败")?;
        self.stream
            .write_all(b"\n")
            .context("写入 audio_frontend control 换行失败")?;
        Ok(())
    }

    fn read_line(&mut self) -> Result<String> {
        let mut chunk = [0u8; CONTROL_READ_CHUNK_BYTES];
        loop {
            if let Some(newline_index) = self.read_buffer.iter().position(|byte| *byte == b'\n') {
                anyhow::ensure!(
                    newline_index <= CONTROL_RESPONSE_MAX_BYTES,
                    "audio_frontend control 响应超过 {CONTROL_RESPONSE_MAX_BYTES} bytes"
                );
                let mut bytes: Vec<u8> = self.read_buffer.drain(..=newline_index).collect();
                bytes.pop();
                if bytes.last() == Some(&b'\r') {
                    bytes.pop();
                }
                return String::from_utf8(bytes).context("audio_frontend control 响应不是 UTF-8");
            }

            anyhow::ensure!(
                self.read_buffer.len() <= CONTROL_RESPONSE_MAX_BYTES,
                "audio_frontend control 响应超过 {CONTROL_RESPONSE_MAX_BYTES} bytes"
            );
            let remaining = CONTROL_RESPONSE_MAX_BYTES + 1 - self.read_buffer.len();
            let read_len = remaining.min(chunk.len());
            match self.stream.read(&mut chunk[..read_len]) {
                Ok(0) => anyhow::bail!("audio_frontend control 连接已关闭"),
                Ok(read) => {
                    self.read_buffer.extend_from_slice(&chunk[..read]);
                }
                Err(err)
                    if err.kind() == io::ErrorKind::WouldBlock
                        || err.kind() == io::ErrorKind::TimedOut =>
                {
                    anyhow::bail!("audio_frontend control 读取超时")
                }
                Err(err) => return Err(err).context("读取 audio_frontend control 响应失败"),
            }
        }
    }
}

fn monotonic_us() -> u64 {
    static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    START
        .get_or_init(Instant::now)
        .elapsed()
        .as_micros()
        .min(u128::from(u64::MAX)) as u64
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{AudioConfig, AudioFrontendMode};
    use std::{
        io::{BufRead, BufReader, Write},
        net::TcpListener,
        thread,
    };

    #[test]
    fn control_sends_json_lines() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind listener");
        let port = listener.local_addr().expect("local addr").port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept client");
            let mut reader = BufReader::new(stream);
            let mut lines = Vec::new();
            for _ in 0..5 {
                let mut line = String::new();
                reader.read_line(&mut line).expect("read line");
                lines.push(line);
            }
            lines
        });

        let audio = test_audio_config(port);
        let mut control = AudioFrontendControl::connect(&audio).expect("connect control");
        control.set_mic_output_muted(true).expect("mute");
        control.speaking_started().expect("started");
        control.playback_interrupt().expect("interrupt");
        control.speaking_finished().expect("finished");
        control.ping().expect("ping");
        drop(control);

        let lines = server.join().expect("server thread");
        assert!(lines[0].contains(r#""type":"mute_mic_output""#));
        assert!(lines[0].contains(r#""enabled":true"#));
        assert!(lines[1].contains(r#""type":"speaking_started""#));
        assert!(lines[2].contains(r#""type":"playback_interrupt""#));
        assert!(lines[3].contains(r#""type":"speaking_finished""#));
        assert!(lines[4].contains(r#""type":"ping""#));
    }

    #[test]
    fn control_restores_speaking_and_mute_state_after_reconnect() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind listener");
        let port = listener.local_addr().expect("local addr").port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept client");
            let mut reader = BufReader::new(stream);
            let mut lines = Vec::new();
            for _ in 0..2 {
                let mut line = String::new();
                reader.read_line(&mut line).expect("read line");
                lines.push(line);
            }
            lines
        });

        let audio = test_audio_config(port);
        let mut control = AudioFrontendControl::connect(&audio).expect("connect control");
        control
            .restore_speaking_state(true, true)
            .expect("restore speaking state");
        drop(control);

        let lines = server.join().expect("server thread");
        assert!(lines[0].contains(r#""type":"mute_mic_output""#));
        assert!(lines[0].contains(r#""enabled":true"#));
        assert!(lines[1].contains(r#""type":"speaking_started""#));
    }

    #[test]
    fn control_queries_status_response() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind listener");
        let port = listener.local_addr().expect("local addr").port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let mut request = String::new();
            {
                let mut reader = BufReader::new(stream.try_clone().expect("clone stream"));
                reader.read_line(&mut request).expect("read status request");
            }
            assert!(request.contains(r#""type":"status""#));
            stream
                .write_all(
                    br#"{"type":"status","ok":true,"sample_rate":48000,"channels":1,"frame_ms":10,"processor_backend":"webrtc","processor_aec_active":true,"processor_noise_suppression_active":false,"processor_agc_active":false,"processor_errors":0,"protocol_errors":0,"socket_errors":0}"#,
                )
                .expect("write status");
            stream.write_all(b"\n").expect("write newline");
        });

        let audio = test_audio_config(port);
        let mut control = AudioFrontendControl::connect(&audio).expect("connect control");
        let status = control.query_status().expect("query status");
        drop(control);
        server.join().expect("server thread");

        assert_eq!(status.processor_backend.as_deref(), Some("webrtc"));
        assert_eq!(status.processor_aec_active, Some(true));
        assert_eq!(status.processor_errors, Some(0));
        assert_eq!(status.protocol_errors, Some(0));
        assert_eq!(status.socket_errors, Some(0));
    }

    #[test]
    fn control_accepts_status_response_larger_than_4k() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind listener");
        let port = listener.local_addr().expect("local addr").port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let mut request = String::new();
            {
                let mut reader = BufReader::new(stream.try_clone().expect("clone stream"));
                reader.read_line(&mut request).expect("read status request");
            }
            let padding = "x".repeat(8 * 1024);
            let response = format!(
                r#"{{"type":"status","ok":true,"sample_rate":48000,"channels":1,"frame_ms":10,"processor_backend":"webrtc","processor_errors":0,"protocol_errors":2,"socket_errors":3,"padding":"{padding}"}}"#
            );
            assert!(response.len() > 4096);
            stream
                .write_all(response.as_bytes())
                .expect("write large status");
            stream.write_all(b"\n").expect("write newline");
        });

        let audio = test_audio_config(port);
        let mut control = AudioFrontendControl::connect(&audio).expect("connect control");
        let status = control.query_status().expect("query large status");
        drop(control);
        server.join().expect("server thread");

        assert_eq!(status.sample_rate, Some(WIRE_SAMPLE_RATE));
        assert_eq!(status.channels, Some(WIRE_CHANNELS));
        assert_eq!(status.frame_ms, Some(WIRE_FRAME_MS));
        assert_eq!(status.protocol_errors, Some(2));
        assert_eq!(status.socket_errors, Some(3));
    }

    #[test]
    fn control_rejects_incompatible_wire_shape() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind listener");
        let port = listener.local_addr().expect("local addr").port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let mut request = String::new();
            {
                let mut reader = BufReader::new(stream.try_clone().expect("clone stream"));
                reader.read_line(&mut request).expect("read status request");
            }
            stream
                .write_all(
                    br#"{"type":"status","ok":true,"sample_rate":16000,"channels":1,"frame_ms":10}"#,
                )
                .expect("write status");
            stream.write_all(b"\n").expect("write newline");
        });

        let audio = test_audio_config(port);
        let mut control = AudioFrontendControl::connect(&audio).expect("connect control");
        let err = control.query_status().expect_err("wire shape must match");
        drop(control);
        server.join().expect("server thread");

        assert!(err.to_string().contains("sample_rate"));
    }

    fn test_audio_config(control_port: u16) -> AudioConfig {
        AudioConfig {
            sample_rate: 16_000,
            channels: 1,
            frame_duration_ms: 10,
            tts_source_rate: 16_000,
            playback_buffer_frames: 2048,
            frontend_mode: AudioFrontendMode::Tcp,
            frontend_host: "127.0.0.1".to_string(),
            frontend_mic_port: 39_001,
            frontend_speaker_port: 39_002,
            frontend_control_port: control_port,
            frontend_mute_mic_during_playback: false,
            tts_barge_in_with_aec: true,
            natural_barge_in_enabled: false,
            frontend_upsampler: crate::config::AudioFrontendUpsampler::LinearLookahead,
            mic_device_name: None,
            speaker_device_name: None,
            pause_capture_while_waiting: true,
            wake_capture_prebuffer_ms: 800,
        }
    }
}
