use crate::transport::{TransportPolicy, WebRtcAudioUplinkMode};
use anyhow::{ensure, Result};
use serde::Serialize;
use std::env;
use std::path::{Path, PathBuf};

const INTERNAL_SAMPLE_RATE: u32 = 16_000;
const INTERNAL_CHANNELS: u16 = 1;
const FRAME_DURATION_MS: u32 = 10;
const TTS_SOURCE_RATE: u32 = 16_000;
const DEFAULT_GATEWAY_SCHEME: &str = "ws";
const DEFAULT_GATEWAY_HOST: &str = "127.0.0.1";
const DEFAULT_GATEWAY_PORT: u32 = 8282;
const DEFAULT_GATEWAY_PATH: &str = "/ws";
const DEFAULT_BOT_ID: &str = "xiaowen";

#[derive(Debug, Clone)]
pub struct Config {
    pub gateway: GatewayConfig,
    pub audio: AudioConfig,
    pub audio_control: AudioControlConfig,
    pub environment_trigger: EnvironmentTriggerConfig,
    pub feedback: FeedbackConfig,
    pub vad: VadConfig,
    pub turn_gate_shadow: TurnGateShadowConfig,
    pub tts: TtsConfig,
    pub transport: TransportConfig,
    pub vision: VisionConfig,
    pub runtime: RuntimeConfig,
    pub wake: WakeConfig,
}

#[derive(Debug, Clone)]
pub struct GatewayConfig {
    pub url: String,
    pub bot_id: String,
    pub robot_id: Option<String>,
    pub robot_secret: Option<String>,
    pub heartbeat_interval_secs: u64,
    pub connect_timeout_secs: f32,
    pub send_timeout_secs: f32,
}

#[derive(Debug, Clone, Serialize)]
pub struct AudioConfig {
    pub sample_rate: u32,
    pub channels: u16,
    pub frame_duration_ms: u32,
    pub tts_source_rate: u32,
    pub playback_buffer_frames: u32,
    pub frontend_mode: AudioFrontendMode,
    pub frontend_host: String,
    pub frontend_mic_port: u16,
    pub frontend_speaker_port: u16,
    pub frontend_control_port: u16,
    pub frontend_mute_mic_during_playback: bool,
    pub tts_barge_in_with_aec: bool,
    pub natural_barge_in_enabled: bool,
    pub frontend_upsampler: AudioFrontendUpsampler,
    pub mic_device_name: Option<String>,
    pub speaker_device_name: Option<String>,
    pub pause_capture_while_waiting: bool,
    pub wake_capture_prebuffer_ms: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AudioFrontendMode {
    Cpal,
    Tcp,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AudioFrontendUpsampler {
    Repeat,
    Linear,
    LinearLookahead,
    Sinc,
}

impl AudioFrontendUpsampler {
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().replace('-', "_").as_str() {
            "" | "repeat" | "nearest" | "zero_order_hold" | "zoh" => Ok(Self::Repeat),
            "linear_lookahead" | "lookahead" | "linear_next" => Ok(Self::LinearLookahead),
            "sinc" | "rubato" | "high_quality" | "hq" => Ok(Self::Sinc),
            "linear" => Ok(Self::Linear),
            other => anyhow::bail!(
                "AUDIO_FRONTEND_UPSAMPLER 不支持: {other}，可选值: repeat, sinc, linear_lookahead, linear"
            ),
        }
    }
}

impl AudioFrontendMode {
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "" | "cpal" | "system" => Ok(Self::Cpal),
            "tcp" | "audio_frontend" | "audio-frontend" => Ok(Self::Tcp),
            other => anyhow::bail!("AUDIO_FRONTEND_MODE 不支持: {other}，可选值: cpal, tcp"),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Cpal => "cpal",
            Self::Tcp => "tcp",
        }
    }
}

impl AudioConfig {
    pub fn should_pause_capture_while_waiting(&self) -> bool {
        self.pause_capture_while_waiting
    }

    pub fn should_detect_tts_barge_in(&self) -> bool {
        self.tts_barge_in_with_aec
            && self.frontend_mode == AudioFrontendMode::Tcp
            && !self.frontend_mute_mic_during_playback
    }

    pub fn should_detect_natural_barge_in(&self) -> bool {
        self.natural_barge_in_enabled && !self.frontend_mute_mic_during_playback
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct AudioControlConfig {
    pub host: String,
    pub port: u16,
    pub default_ttl_ms: u64,
    pub max_ttl_ms: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct EnvironmentTriggerConfig {
    pub host: String,
    pub port: u16,
    pub path: String,
    pub template: String,
    pub debounce_secs: f32,
    pub max_content_chars: usize,
    pub perception_start_url: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FeedbackConfig {
    pub asr_ding_sound_path: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct VadConfig {
    pub silence_threshold_secs: f32,
    pub speech_threshold_secs: f32,
    pub speech_trigger_ratio: f32,
    pub speech_start_min_dbfs: f32,
    pub max_recording_duration_secs: u64,
    pub mode: u8,
}

#[derive(Debug, Clone, Serialize)]
pub struct TurnGateShadowConfig {
    pub enabled: bool,
    pub active_enabled: bool,
    pub candidate_silence_ms: u64,
    pub min_commit_silence_ms: u64,
    pub failure_fallback_ms: u64,
    pub ambiguous_hard_timeout_ms: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct TtsConfig {
    pub prebuffer_secs: f32,
    pub volume_gain: f32,
    pub agc_target_db: f32,
}

#[derive(Debug, Clone)]
pub struct TransportConfig {
    pub policy: TransportPolicy,
    pub webrtc_enabled: bool,
    pub webrtc_connect_timeout_ms: u64,
    pub fallback_enabled: bool,
    pub webrtc_audio_uplink_mode: WebRtcAudioUplinkMode,
}

#[derive(Debug, Clone, Serialize)]
pub struct VisionConfig {
    pub enabled: bool,
    pub snapshot_path: String,
    pub scan_interval_ms: u64,
}

impl Default for VisionConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            snapshot_path: "temp/pic.jpeg".to_string(),
            scan_interval_ms: 3000,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct WakeConfig {
    pub source_mode: WakeSourceMode,
    pub serial_port: Option<String>,
    pub serial_baud: u32,
    pub serial_timeout_secs: f32,
    pub serial_reconnect_secs: f32,
    pub debounce_secs: f32,
    pub http_enabled: bool,
    pub http_host: String,
    pub http_port: u16,
    pub http_path: String,
    pub kws_host: String,
    pub kws_port: u16,
    pub kws_reconnect_secs: f32,
    pub angle_report_url: String,
    pub angle_report_timeout_secs: f32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WakeSourceMode {
    Hardware,
    Kws,
    Both,
}

impl WakeSourceMode {
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "hardware" | "serial" => Ok(Self::Hardware),
            "kws" | "tcp_kws" | "kws_tcp" => Ok(Self::Kws),
            "both" | "all" => Ok(Self::Both),
            other => {
                anyhow::bail!("WAKE_SOURCE_MODE must be hardware, kws, or both; got {other:?}")
            }
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Hardware => "hardware",
            Self::Kws => "kws",
            Self::Both => "both",
        }
    }

    pub fn hardware_enabled(self) -> bool {
        matches!(self, Self::Hardware | Self::Both)
    }

    pub fn kws_enabled(self) -> bool {
        matches!(self, Self::Kws | Self::Both)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct RuntimeConfig {
    pub session_timeout_secs: u64,
    pub response_timeout_secs: u64,
    pub tts_playback_timeout_secs: u64,
}

#[derive(Debug, Serialize)]
pub struct PrintableConfig {
    pub gateway: PrintableGatewayConfig,
    pub audio: AudioConfig,
    pub audio_control: AudioControlConfig,
    pub environment_trigger: EnvironmentTriggerConfig,
    pub feedback: FeedbackConfig,
    pub vad: VadConfig,
    pub turn_gate_shadow: TurnGateShadowConfig,
    pub tts: TtsConfig,
    pub transport: PrintableTransportConfig,
    pub vision: VisionConfig,
    pub runtime: RuntimeConfig,
    pub wake: WakeConfig,
}

#[derive(Debug, Serialize)]
pub struct PrintableGatewayConfig {
    pub url: String,
    pub bot_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub robot_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub robot_secret: Option<&'static str>,
    pub heartbeat_interval_secs: u64,
    pub connect_timeout_secs: f32,
    pub send_timeout_secs: f32,
}

#[derive(Debug, Serialize)]
pub struct PrintableTransportConfig {
    pub policy: &'static str,
    pub webrtc_enabled: bool,
    pub webrtc_connect_timeout_ms: u64,
    pub fallback_enabled: bool,
    pub webrtc_audio_uplink_mode: &'static str,
}

pub struct EnvHelpEntry {
    pub field: &'static str,
    pub env: &'static str,
    pub default: &'static str,
    pub example: &'static str,
    pub notes: &'static str,
}

pub fn env_help_text() -> String {
    render_env_help(
        "Rust Client environment variables",
        &rust_client_env_help_entries(),
    )
}

fn rust_client_env_help_entries() -> Vec<EnvHelpEntry> {
    vec![
        EnvHelpEntry { field: "gateway.url", env: "GATEWAY_URL or GATEWAY_SCHEME + GATEWAY_HOST + GATEWAY_PORT + GATEWAY_PATH", default: "ws://127.0.0.1:8282/ws", example: "export GATEWAY_URL=ws://<server-ip>:8282/ws", notes: "GATEWAY_URL takes precedence and must start with ws:// or wss://." },
        EnvHelpEntry { field: "gateway.bot_id", env: "BOT_ID", default: DEFAULT_BOT_ID, example: "export BOT_ID=xiaowen", notes: "Bot id sent during register." },
        EnvHelpEntry { field: "gateway.robot_id", env: "ROBOT_ID", default: "<required>", example: "export ROBOT_ID=test_01", notes: "Required for the production Rust client." },
        EnvHelpEntry { field: "gateway.robot_secret", env: "ROBOT_SECRET", default: "<empty>", example: "export ROBOT_SECRET=...", notes: "Optional locally, required when Gateway strong auth is enabled; redacted by --print-config." },
        EnvHelpEntry { field: "gateway.heartbeat_interval_secs", env: "HEARTBEAT_INTERVAL", default: "30", example: "export HEARTBEAT_INTERVAL=30", notes: "Heartbeat interval sent over the active transport." },
        EnvHelpEntry { field: "gateway.connect_timeout_secs", env: "GATEWAY_CONNECT_TIMEOUT", default: "8.0", example: "export GATEWAY_CONNECT_TIMEOUT=8", notes: "Gateway connect timeout in seconds." },
        EnvHelpEntry { field: "gateway.send_timeout_secs", env: "GATEWAY_SEND_TIMEOUT", default: "3.0", example: "export GATEWAY_SEND_TIMEOUT=3", notes: "Gateway send/write timeout in seconds." },
        EnvHelpEntry { field: "audio.playback_buffer_frames", env: "PLAYBACK_FRAMES_PER_BUFFER", default: "2048", example: "export PLAYBACK_FRAMES_PER_BUFFER=2048", notes: "Audio output buffer size; values below 512 are clamped." },
        EnvHelpEntry { field: "audio.frontend_mode", env: "AUDIO_FRONTEND_MODE", default: "cpal", example: "export AUDIO_FRONTEND_MODE=tcp", notes: "cpal keeps direct system audio. tcp reads mic PCM from and sends playback PCM to the local C++ audio_frontend." },
        EnvHelpEntry { field: "audio.frontend_host", env: "AUDIO_FRONTEND_HOST", default: "127.0.0.1", example: "export AUDIO_FRONTEND_HOST=127.0.0.1", notes: "Local C++ audio_frontend host for tcp mode." },
        EnvHelpEntry { field: "audio.frontend_mic_port", env: "AUDIO_FRONTEND_MIC_PORT", default: "39001", example: "export AUDIO_FRONTEND_MIC_PORT=39001", notes: "Mic PCM port exposed by audio_frontend." },
        EnvHelpEntry { field: "audio.frontend_speaker_port", env: "AUDIO_FRONTEND_SPEAKER_PORT", default: "39002", example: "export AUDIO_FRONTEND_SPEAKER_PORT=39002", notes: "Speaker PCM port exposed by audio_frontend." },
        EnvHelpEntry { field: "audio.frontend_control_port", env: "AUDIO_FRONTEND_CONTROL_PORT", default: "39003", example: "export AUDIO_FRONTEND_CONTROL_PORT=39003", notes: "Optional JSONL control port exposed by audio_frontend." },
        EnvHelpEntry { field: "audio.frontend_mute_mic_during_playback", env: "AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK", default: "false", example: "export AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK=false", notes: "Optional half-duplex guard. Keep false for the AEC path so mic frames remain available during TTS playback." },
        EnvHelpEntry { field: "audio.frontend_upsampler", env: "AUDIO_FRONTEND_UPSAMPLER", default: "repeat", example: "export AUDIO_FRONTEND_UPSAMPLER=repeat", notes: "TCP speaker 16k->48k upsampler. repeat is the stable low-latency path. sinc has filter delay and, together with linear_lookahead and linear, is intended only for explicit A/B tests." },
        EnvHelpEntry { field: "audio.tts_barge_in_with_aec", env: "TTS_BARGE_IN_WITH_AEC", default: "false", example: "export TTS_BARGE_IN_WITH_AEC=true", notes: "Experimental opt-in speech barge-in while TTS is playing. Effective only in tcp mode when mic output is not muted." },
        EnvHelpEntry { field: "audio.natural_barge_in_enabled", env: "NATURAL_BARGE_IN_ENABLED", default: "false", example: "export NATURAL_BARGE_IN_ENABLED=true", notes: "Opt-in ASR-confirmed natural barge-in for hardware-cleaned system audio or the software AEC path. Uses the existing audio uplink and requires mic capture during playback." },
        EnvHelpEntry { field: "audio.mic_device_name", env: "MIC_DEVICE_NAME", default: "<system default>", example: "export MIC_DEVICE_NAME='MacBook Pro Microphone'", notes: "Optional substring/device name for input selection." },
        EnvHelpEntry { field: "audio.speaker_device_name", env: "SPEAKER_DEVICE_NAME", default: "<system default>", example: "export SPEAKER_DEVICE_NAME='MacBook Pro Speakers'", notes: "Optional substring/device name for output selection." },
        EnvHelpEntry { field: "audio.pause_capture_while_waiting", env: "PAUSE_CAPTURE_WHILE_WAITING", default: "true", example: "export PAUSE_CAPTURE_WHILE_WAITING=true", notes: "Pause RustClient capture while waiting for wake. In tcp mode, the C++ audio_frontend can keep owning the system mic while RustClient connects after wake." },
        EnvHelpEntry { field: "audio.wake_capture_prebuffer_ms", env: "WAKE_CAPTURE_PREBUFFER_MS", default: "800", example: "export WAKE_CAPTURE_PREBUFFER_MS=800", notes: "Recording preroll kept after wake before VAD triggers; set 0 to disable." },
        EnvHelpEntry { field: "audio.opus_bitrate", env: "OPUS_BITRATE_BPS", default: "24000", example: "export OPUS_BITRATE_BPS=24000", notes: "Opus encoder bitrate for uplink audio." },
        EnvHelpEntry { field: "tts.prebuffer_secs", env: "TTS_PREBUFFER_SEC", default: "0.10", example: "export TTS_PREBUFFER_SEC=0.10", notes: "Small TTS playback prebuffer; keep low for first-audio latency." },
        EnvHelpEntry { field: "tts.volume_gain", env: "VOLUME_GAIN", default: "2.0", example: "export VOLUME_GAIN=2.0", notes: "Playback gain before AGC." },
        EnvHelpEntry { field: "tts.agc_target_db", env: "AGC_TARGET_DB", default: "-25.0", example: "export AGC_TARGET_DB=-25", notes: "Playback AGC target." },
        EnvHelpEntry { field: "transport.policy", env: "TRANSPORT_POLICY", default: "webrtc_only", example: "export TRANSPORT_POLICY=webrtc_only", notes: "Valid values: websocket_only, webrtc_only, auto_prefer_webrtc." },
        EnvHelpEntry { field: "transport.webrtc_enabled", env: "WEBRTC_ENABLED", default: "true", example: "export WEBRTC_ENABLED=true", notes: "Must stay true for WebRTC-only transport." },
        EnvHelpEntry { field: "transport.webrtc_connect_timeout_ms", env: "WEBRTC_CONNECT_TIMEOUT_MS", default: "15000", example: "export WEBRTC_CONNECT_TIMEOUT_MS=15000", notes: "WebRTC negotiation/connect timeout." },
        EnvHelpEntry { field: "transport.fallback_enabled", env: "TRANSPORT_FALLBACK_ENABLED", default: "false", example: "export TRANSPORT_FALLBACK_ENABLED=false", notes: "Keep false for v3 production; true is a diagnostic/transition switch that can fall back to the legacy WS audio path when policy permits it." },
        EnvHelpEntry { field: "transport.webrtc_audio_uplink_mode", env: "RTC_AUDIO_UPLINK", default: "rtp_only", example: "export RTC_AUDIO_UPLINK=rtp_only", notes: "Valid values: rtp_only, mirror_ws." },
        EnvHelpEntry { field: "vision.enabled", env: "VISION_SNAPSHOT_ENABLED", default: "false", example: "export VISION_SNAPSHOT_ENABLED=true", notes: "Enable the dedicated vision-v1 DataChannel snapshot sender." },
        EnvHelpEntry { field: "vision.snapshot_path", env: "VISION_SNAPSHOT_PATH", default: "temp/pic.jpeg", example: "export VISION_SNAPSHOT_PATH=/opt/robot/temp/pic.jpeg", notes: "Producer must publish a complete JPEG with an atomic sibling-file rename." },
        EnvHelpEntry { field: "vision.scan_interval_ms", env: "VISION_SNAPSHOT_SCAN_INTERVAL_MS", default: "3000", example: "export VISION_SNAPSHOT_SCAN_INTERVAL_MS=3000", notes: "Read-pause interval after each snapshot cycle; upload cadence is limited by snapshot producer + this pause." },
        EnvHelpEntry { field: "runtime.session_timeout_secs", env: "SESSION_TIMEOUT", default: "15", example: "export SESSION_TIMEOUT=15", notes: "Idle session timeout before returning to wake state." },
        EnvHelpEntry { field: "runtime.response_timeout_secs", env: "RESPONSE_TIMEOUT", default: "45", example: "export RESPONSE_TIMEOUT=45", notes: "Max wait for a server response before recovering." },
        EnvHelpEntry { field: "runtime.tts_playback_timeout_secs", env: "TTS_PLAYBACK_TIMEOUT", default: "60", example: "export TTS_PLAYBACK_TIMEOUT=60", notes: "Max playback round duration." },
        EnvHelpEntry { field: "wake.serial_port", env: "WAKE_SERIAL_PORT", default: "<auto>", example: "export WAKE_SERIAL_PORT=/dev/cu.usbserial-110", notes: "Optional hardware wake serial port." },
        EnvHelpEntry { field: "wake.source_mode", env: "WAKE_SOURCE_MODE", default: "hardware", example: "export WAKE_SOURCE_MODE=both", notes: "Wake source selection: hardware, kws, or both. hardware keeps the existing serial + HTTP wake behavior." },
        EnvHelpEntry { field: "wake.serial_baud", env: "WAKE_SERIAL_BAUD", default: "115200", example: "export WAKE_SERIAL_BAUD=115200", notes: "Hardware wake serial baud rate." },
        EnvHelpEntry { field: "wake.serial_timeout_secs", env: "WAKE_SERIAL_TIMEOUT", default: "0.2", example: "export WAKE_SERIAL_TIMEOUT=0.2", notes: "Serial read timeout." },
        EnvHelpEntry { field: "wake.serial_reconnect_secs", env: "WAKE_SERIAL_RECONNECT_SEC", default: "1.0", example: "export WAKE_SERIAL_RECONNECT_SEC=1.0", notes: "Serial reconnect interval." },
        EnvHelpEntry { field: "wake.debounce_secs", env: "WAKE_DEBOUNCE_SEC", default: "0.5", example: "export WAKE_DEBOUNCE_SEC=0.5", notes: "Global wake-state debounce interval shared by hardware, HTTP, and KWS wake events." },
        EnvHelpEntry { field: "wake.http_enabled", env: "WAKE_HTTP_ENABLED", default: "true", example: "export WAKE_HTTP_ENABLED=true", notes: "Enable local HTTP wake endpoint." },
        EnvHelpEntry { field: "wake.http_host", env: "WAKE_HTTP_HOST", default: "127.0.0.1", example: "export WAKE_HTTP_HOST=0.0.0.0", notes: "HTTP wake bind host." },
        EnvHelpEntry { field: "wake.http_port", env: "WAKE_HTTP_PORT", default: "5202", example: "export WAKE_HTTP_PORT=5202", notes: "HTTP wake bind port." },
        EnvHelpEntry { field: "wake.http_path", env: "WAKE_HTTP_PATH", default: "/wakeup", example: "export WAKE_HTTP_PATH=/wakeup", notes: "HTTP wake path." },
        EnvHelpEntry { field: "wake.kws_host", env: "KWS_WAKE_HOST", default: "127.0.0.1", example: "export KWS_WAKE_HOST=127.0.0.1", notes: "audio_frontend KWS event TCP host." },
        EnvHelpEntry { field: "wake.kws_port", env: "KWS_WAKE_PORT", default: "39004", example: "export KWS_WAKE_PORT=39004", notes: "audio_frontend KWS event TCP port." },
        EnvHelpEntry { field: "wake.kws_reconnect_secs", env: "KWS_WAKE_RECONNECT_SEC", default: "1.0", example: "export KWS_WAKE_RECONNECT_SEC=1.0", notes: "Reconnect interval for the KWS event TCP client." },
        EnvHelpEntry { field: "wake.angle_report_url", env: "ANGLE_REPORT_URL", default: "http://127.0.0.1:19992/api/robot/angle", example: "export ANGLE_REPORT_URL=http://127.0.0.1:19992/api/robot/angle", notes: "Optional wake-angle report endpoint." },
        EnvHelpEntry { field: "wake.angle_report_timeout_secs", env: "ANGLE_REPORT_TIMEOUT", default: "1.0", example: "export ANGLE_REPORT_TIMEOUT=1", notes: "Wake-angle report timeout." },
        EnvHelpEntry { field: "vad.silence_threshold_secs", env: "VAD_SILENCE_THRESHOLD", default: "0.5", example: "export VAD_SILENCE_THRESHOLD=0.5", notes: "Silence duration before ending recording." },
        EnvHelpEntry { field: "vad.speech_threshold_secs", env: "VAD_SPEECH_THRESHOLD", default: "0.25", example: "export VAD_SPEECH_THRESHOLD=0.25", notes: "Speech trigger window duration." },
        EnvHelpEntry { field: "vad.speech_trigger_ratio", env: "VAD_SPEECH_TRIGGER_RATIO", default: "0.6", example: "export VAD_SPEECH_TRIGGER_RATIO=0.6", notes: "Required speech ratio in the trigger window, clamped to 0.5-1.0." },
        EnvHelpEntry { field: "vad.speech_start_min_dbfs", env: "VAD_SPEECH_START_MIN_DBFS", default: "-36.0", example: "export VAD_SPEECH_START_MIN_DBFS=-36", notes: "Minimum level for a WebRTC VAD speech frame to count as speech." },
        EnvHelpEntry { field: "vad.max_recording_duration_secs", env: "VAD_MAX_RECORDING_DURATION", default: "10", example: "export VAD_MAX_RECORDING_DURATION=10", notes: "Maximum user recording duration." },
        EnvHelpEntry { field: "vad.mode", env: "VAD_MODE", default: "2", example: "export VAD_MODE=2", notes: "WebRTC VAD mode, 0-3; higher is more aggressive and may reject quiet speech." },
        EnvHelpEntry { field: "turn_gate_shadow.enabled", env: "TURN_GATE_SHADOW_ENABLED", default: "false", example: "export TURN_GATE_SHADOW_ENABLED=true", notes: "Emit provisional turn candidates without changing audio_end behavior." },
        EnvHelpEntry { field: "turn_gate_shadow.active_enabled", env: "TURN_GATE_ACTIVE_ENABLED", default: "true", example: "export TURN_GATE_ACTIVE_ENABLED=false", notes: "Accept cloud turn_commit and mark candidates active; enabled by default on the Rust client and can be explicitly disabled for rollback." },
        EnvHelpEntry { field: "turn_gate_shadow.candidate_silence_ms", env: "TURN_GATE_CANDIDATE_SILENCE_MS", default: "300", example: "export TURN_GATE_CANDIDATE_SILENCE_MS=300", notes: "Observed silence before emitting one shadow candidate for the current silence epoch." },
        EnvHelpEntry { field: "turn_gate_shadow.min_commit_silence_ms", env: "TURN_GATE_MIN_COMMIT_SILENCE_MS", default: "600", example: "export TURN_GATE_MIN_COMMIT_SILENCE_MS=600", notes: "Minimum uninterrupted silence before accepting the first valid active double-true commit." },
        EnvHelpEntry { field: "turn_gate_shadow.failure_fallback_ms", env: "TURN_GATE_FAILURE_FALLBACK_MS", default: "1000", example: "export TURN_GATE_FAILURE_FALLBACK_MS=1000", notes: "Active-mode silence fallback when no healthy semantic continue decision arrives." },
        EnvHelpEntry { field: "turn_gate_shadow.ambiguous_hard_timeout_ms", env: "TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS", default: "1600", example: "export TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS=1600", notes: "Active-mode maximum silence after Smart Turn or EOU explicitly says continue listening." },
        EnvHelpEntry { field: "audio_control.host", env: "AUDIO_CONTROL_HOST", default: "127.0.0.1", example: "export AUDIO_CONTROL_HOST=127.0.0.1", notes: "Local audio-ducking control bind host." },
        EnvHelpEntry { field: "audio_control.port", env: "AUDIO_CONTROL_PORT", default: "19994", example: "export AUDIO_CONTROL_PORT=19994", notes: "Local audio-ducking control port." },
        EnvHelpEntry { field: "audio_control.default_ttl_ms", env: "AUDIO_DUCK_DEFAULT_TTL_MS", default: "15000", example: "export AUDIO_DUCK_DEFAULT_TTL_MS=15000", notes: "Default ducking TTL." },
        EnvHelpEntry { field: "audio_control.max_ttl_ms", env: "AUDIO_DUCK_MAX_TTL_MS", default: "60000", example: "export AUDIO_DUCK_MAX_TTL_MS=60000", notes: "Maximum ducking TTL." },
        EnvHelpEntry { field: "environment_trigger.host", env: "ENV_TALK_HOST", default: "127.0.0.1", example: "export ENV_TALK_HOST=127.0.0.1", notes: "Local active-talk trigger host." },
        EnvHelpEntry { field: "environment_trigger.port", env: "ENV_TALK_PORT", default: "5201", example: "export ENV_TALK_PORT=5201", notes: "Local active-talk trigger port." },
        EnvHelpEntry { field: "environment_trigger.path", env: "ENV_TALK_PATH", default: "/api/robot/start_talk", example: "export ENV_TALK_PATH=/api/robot/start_talk", notes: "Local active-talk trigger path." },
        EnvHelpEntry { field: "environment_trigger.template", env: "ENV_TALK_TEMPLATE", default: "请根据环境主动回应", example: "export ENV_TALK_TEMPLATE=请根据环境主动回应", notes: "Template used for environment-triggered speech." },
        EnvHelpEntry { field: "environment_trigger.debounce_secs", env: "ENV_TALK_DEBOUNCE_SEC", default: "5.0", example: "export ENV_TALK_DEBOUNCE_SEC=5", notes: "Active-talk debounce interval." },
        EnvHelpEntry { field: "environment_trigger.max_content_chars", env: "ENV_TALK_MAX_CONTENT_CHARS", default: "500", example: "export ENV_TALK_MAX_CONTENT_CHARS=500", notes: "Max active-talk content length." },
        EnvHelpEntry { field: "environment_trigger.perception_start_url", env: "ENV_PERCEPTION_START_URL", default: "http://127.0.0.1:5200/api/robot/start_vision", example: "export ENV_PERCEPTION_START_URL=http://127.0.0.1:5200/api/robot/start_vision", notes: "Optional perception trigger endpoint." },
        EnvHelpEntry { field: "feedback.asr_ding_sound_path", env: "ASR_DING_SOUND_PATH", default: "first bundled ding.wav found", example: "export ASR_DING_SOUND_PATH=rust_client/assets/wake_audio/ding.wav", notes: "ASR confirmation sound." },
        EnvHelpEntry { field: "logging.level", env: "VOICE_LOG_LEVEL, RUST_LOG", default: "info", example: "export VOICE_LOG_LEVEL=info", notes: "Tracing filter." },
        EnvHelpEntry { field: "logging.format", env: "VOICE_LOG_FORMAT, RUST_LOG_FORMAT", default: "compact", example: "export VOICE_LOG_FORMAT=compact", notes: "Valid values: compact, pretty, full." },
        EnvHelpEntry { field: "logging.color", env: "VOICE_LOG_COLOR", default: "auto", example: "export VOICE_LOG_COLOR=auto", notes: "Use true/false or auto." },
        EnvHelpEntry { field: "webrtc.offer_factory", env: "WEBRTC_OFFER_FACTORY", default: "native", example: "export WEBRTC_OFFER_FACTORY=native", notes: "Use native with the native-webrtc feature; helper can use WEBRTC_OFFER_HELPER." },
        EnvHelpEntry { field: "webrtc.offer_helper", env: "WEBRTC_OFFER_HELPER", default: "<empty>", example: "export WEBRTC_OFFER_HELPER=/path/to/helper", notes: "External SDP offer helper command when not using native factory." },
        EnvHelpEntry { field: "webrtc.offer_helper_timeout_ms", env: "WEBRTC_OFFER_HELPER_TIMEOUT_MS", default: "5000", example: "export WEBRTC_OFFER_HELPER_TIMEOUT_MS=5000", notes: "External offer helper timeout." },
        EnvHelpEntry { field: "webrtc.native_enabled", env: "WEBRTC_NATIVE_WEBRTC_ENABLED", default: "false", example: "export WEBRTC_NATIVE_WEBRTC_ENABLED=true", notes: "Legacy flag that also requests native offer factory." },
        EnvHelpEntry { field: "webrtc.native_gather_timeout_ms", env: "WEBRTC_NATIVE_GATHER_TIMEOUT_MS", default: "5000", example: "export WEBRTC_NATIVE_GATHER_TIMEOUT_MS=5000", notes: "Native ICE gathering timeout." },
        EnvHelpEntry { field: "webrtc.native_ice_network_types", env: "WEBRTC_NATIVE_ICE_NETWORK_TYPES", default: "udp4", example: "export WEBRTC_NATIVE_ICE_NETWORK_TYPES=udp4", notes: "Valid values: udp4, udp4,udp6, all. ICE servers are received from Go Gateway; current Rust native keeps STUN/TURN UDP and logs skipped TURN TCP/TLS URLs." },
        EnvHelpEntry { field: "webrtc.native_rtp_probe_enabled", env: "WEBRTC_NATIVE_RTP_PROBE_ENABLED", default: "false", example: "export WEBRTC_NATIVE_RTP_PROBE_ENABLED=false", notes: "Send a tiny RTP probe after native setup." },
    ]
}

fn render_env_help(title: &str, entries: &[EnvHelpEntry]) -> String {
    let mut out = String::new();
    out.push_str(title);
    out.push_str("\n\nUsage:\n");
    out.push_str("  export NAME=value\n");
    out.push_str(
        "  NAME=value cargo run --bin rust_client --features native-webrtc -- --print-config\n\n",
    );
    for entry in entries {
        out.push_str(entry.field);
        out.push('\n');
        out.push_str("  env:     ");
        out.push_str(entry.env);
        out.push('\n');
        out.push_str("  default: ");
        out.push_str(entry.default);
        out.push('\n');
        out.push_str("  example: ");
        out.push_str(entry.example);
        out.push('\n');
        out.push_str("  notes:   ");
        out.push_str(entry.notes);
        out.push_str("\n\n");
    }
    out
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let audio_duck_default_ttl_ms = get_env_u64("AUDIO_DUCK_DEFAULT_TTL_MS", 15_000).max(1);
        let audio_duck_max_ttl_ms =
            get_env_u64("AUDIO_DUCK_MAX_TTL_MS", 60_000).max(audio_duck_default_ttl_ms);
        let audio_control_host = env::var("AUDIO_CONTROL_HOST")
            .unwrap_or_else(|_| "127.0.0.1".to_string())
            .trim()
            .to_string();
        let audio_control_host = if audio_control_host.is_empty() {
            "127.0.0.1".to_string()
        } else {
            audio_control_host
        };
        let robot_id = env::var("ROBOT_ID")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty());
        let effective_gateway_url = env::var("GATEWAY_URL")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or_else(|| format!(
                "{DEFAULT_GATEWAY_SCHEME}://{DEFAULT_GATEWAY_HOST}:{DEFAULT_GATEWAY_PORT}{DEFAULT_GATEWAY_PATH}"
            ));
        let effective_bot_id = env::var("BOT_ID")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_BOT_ID.to_string());
        ensure!(
            robot_id.is_some(),
            "ROBOT_ID 未设置或为空。Rust Client 要求先在 .env / 环境变量中配置 Robot 身份。\n\
             请复制 .env.example 为 .env 并填写 ROBOT_ID（以及可选的 ROBOT_SECRET），\n\
             或参考 START.md 的『Robot 身份配置』章节。\n\
             当前已加载配置: GATEWAY_URL={effective_gateway_url}, BOT_ID={effective_bot_id}",
        );
        let audio_frontend_mode =
            AudioFrontendMode::parse(&get_env_non_empty("AUDIO_FRONTEND_MODE", "cpal"))?;
        let turn_gate_active_enabled = get_env_bool("TURN_GATE_ACTIVE_ENABLED", true);
        let turn_gate_shadow_enabled = get_env_bool("TURN_GATE_SHADOW_ENABLED", false);
        let turn_candidate_silence_ms = get_env_u64("TURN_GATE_CANDIDATE_SILENCE_MS", 300).max(200);
        let turn_min_commit_silence_ms =
            get_env_u64("TURN_GATE_MIN_COMMIT_SILENCE_MS", 600).max(turn_candidate_silence_ms);
        let turn_failure_fallback_ms =
            get_env_u64("TURN_GATE_FAILURE_FALLBACK_MS", 1000).max(turn_min_commit_silence_ms);
        let turn_ambiguous_hard_timeout_ms =
            get_env_u64("TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS", 1600).max(turn_failure_fallback_ms);

        Ok(Self {
            gateway: GatewayConfig {
                url: build_gateway_url()?,
                bot_id: env::var("BOT_ID").unwrap_or_else(|_| DEFAULT_BOT_ID.to_string()),
                robot_id,
                robot_secret: env::var("ROBOT_SECRET")
                    .ok()
                    .filter(|s| !s.trim().is_empty()),
                heartbeat_interval_secs: get_env_u64("HEARTBEAT_INTERVAL", 30),
                connect_timeout_secs: get_env_f32("GATEWAY_CONNECT_TIMEOUT", 8.0).max(1.0),
                send_timeout_secs: get_env_f32("GATEWAY_SEND_TIMEOUT", 3.0).max(0.5),
            },
            audio: AudioConfig {
                sample_rate: INTERNAL_SAMPLE_RATE,
                channels: INTERNAL_CHANNELS,
                frame_duration_ms: FRAME_DURATION_MS,
                tts_source_rate: TTS_SOURCE_RATE,
                playback_buffer_frames: get_env_u32("PLAYBACK_FRAMES_PER_BUFFER", 2048).max(512),
                frontend_mode: audio_frontend_mode,
                frontend_host: get_env_non_empty("AUDIO_FRONTEND_HOST", "127.0.0.1"),
                frontend_mic_port: get_env_u16("AUDIO_FRONTEND_MIC_PORT", 39_001).max(1),
                frontend_speaker_port: get_env_u16("AUDIO_FRONTEND_SPEAKER_PORT", 39_002).max(1),
                frontend_control_port: get_env_u16("AUDIO_FRONTEND_CONTROL_PORT", 39_003).max(1),
                frontend_mute_mic_during_playback: get_env_bool(
                    "AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK",
                    false,
                ),
                tts_barge_in_with_aec: get_env_bool("TTS_BARGE_IN_WITH_AEC", false),
                natural_barge_in_enabled: get_env_bool("NATURAL_BARGE_IN_ENABLED", false),
                frontend_upsampler: AudioFrontendUpsampler::parse(&get_env_non_empty(
                    "AUDIO_FRONTEND_UPSAMPLER",
                    "repeat",
                ))?,
                mic_device_name: env::var("MIC_DEVICE_NAME").ok().filter(|s| !s.is_empty()),
                speaker_device_name: env::var("SPEAKER_DEVICE_NAME")
                    .ok()
                    .filter(|s| !s.is_empty()),
                pause_capture_while_waiting: get_env_bool("PAUSE_CAPTURE_WHILE_WAITING", true),
                wake_capture_prebuffer_ms: get_env_u32("WAKE_CAPTURE_PREBUFFER_MS", 800),
            },
            audio_control: AudioControlConfig {
                host: audio_control_host,
                port: get_env_u16("AUDIO_CONTROL_PORT", 19_994).max(1),
                default_ttl_ms: audio_duck_default_ttl_ms.min(audio_duck_max_ttl_ms),
                max_ttl_ms: audio_duck_max_ttl_ms,
            },
            environment_trigger: EnvironmentTriggerConfig {
                host: get_env_non_empty("ENV_TALK_HOST", "127.0.0.1"),
                port: get_env_u16("ENV_TALK_PORT", 5_201).max(1),
                path: normalize_http_path(&get_env_non_empty(
                    "ENV_TALK_PATH",
                    "/api/robot/start_talk",
                )),
                template: get_env_non_empty("ENV_TALK_TEMPLATE", "请根据环境主动回应"),
                debounce_secs: get_env_f32("ENV_TALK_DEBOUNCE_SEC", 5.0).max(0.0),
                max_content_chars: get_env_usize("ENV_TALK_MAX_CONTENT_CHARS", 500).max(1),
                perception_start_url: env::var("ENV_PERCEPTION_START_URL")
                    .unwrap_or_else(|_| "http://127.0.0.1:5200/api/robot/start_vision".to_string())
                    .trim()
                    .to_string()
                    .into_non_empty(),
            },
            feedback: FeedbackConfig {
                asr_ding_sound_path: get_optional_path_env("ASR_DING_SOUND_PATH").or_else(|| {
                    resolve_first_existing_path(&[
                        "rust_client/assets/wake_audio/ding.wav",
                        "assets/wake_audio/ding.wav",
                    ])
                }),
            },
            vad: VadConfig {
                silence_threshold_secs: get_env_f32("VAD_SILENCE_THRESHOLD", 0.5).max(0.2),
                speech_threshold_secs: get_env_f32("VAD_SPEECH_THRESHOLD", 0.25).max(0.1),
                speech_trigger_ratio: get_env_f32("VAD_SPEECH_TRIGGER_RATIO", 0.6).clamp(0.5, 1.0),
                speech_start_min_dbfs: get_env_f32("VAD_SPEECH_START_MIN_DBFS", -36.0)
                    .clamp(-80.0, -5.0),
                max_recording_duration_secs: get_env_u64("VAD_MAX_RECORDING_DURATION", 10).max(5),
                mode: get_env_u8("VAD_MODE", 2).min(3),
            },
            turn_gate_shadow: TurnGateShadowConfig {
                enabled: turn_gate_shadow_enabled || turn_gate_active_enabled,
                active_enabled: turn_gate_active_enabled,
                candidate_silence_ms: turn_candidate_silence_ms,
                min_commit_silence_ms: turn_min_commit_silence_ms,
                failure_fallback_ms: turn_failure_fallback_ms,
                ambiguous_hard_timeout_ms: turn_ambiguous_hard_timeout_ms,
            },
            tts: TtsConfig {
                prebuffer_secs: get_env_f32("TTS_PREBUFFER_SEC", 0.10).max(0.0),
                volume_gain: get_env_f32("VOLUME_GAIN", 2.0),
                agc_target_db: get_env_f32("AGC_TARGET_DB", -25.0),
            },
            transport: TransportConfig {
                policy: TransportPolicy::parse(&get_env_non_empty(
                    "TRANSPORT_POLICY",
                    "webrtc_only",
                ))?,
                webrtc_enabled: get_env_bool("WEBRTC_ENABLED", true),
                webrtc_connect_timeout_ms: get_env_u64("WEBRTC_CONNECT_TIMEOUT_MS", 15_000).max(1),
                fallback_enabled: get_env_bool("TRANSPORT_FALLBACK_ENABLED", false),
                webrtc_audio_uplink_mode: WebRtcAudioUplinkMode::parse(&get_env_non_empty(
                    "RTC_AUDIO_UPLINK",
                    "rtp_only",
                ))?,
            },
            vision: VisionConfig {
                enabled: get_env_bool("VISION_SNAPSHOT_ENABLED", false),
                snapshot_path: get_env_non_empty("VISION_SNAPSHOT_PATH", "temp/pic.jpeg"),
                scan_interval_ms: get_env_u64("VISION_SNAPSHOT_SCAN_INTERVAL_MS", 3000).max(100),
            },
            runtime: RuntimeConfig {
                session_timeout_secs: get_env_u64("SESSION_TIMEOUT", 15),
                response_timeout_secs: get_env_u64("RESPONSE_TIMEOUT", 45).max(5),
                tts_playback_timeout_secs: get_env_u64("TTS_PLAYBACK_TIMEOUT", 60).max(10),
            },
            wake: WakeConfig {
                source_mode: WakeSourceMode::parse(&get_env_non_empty(
                    "WAKE_SOURCE_MODE",
                    "hardware",
                ))?,
                serial_port: env::var("WAKE_SERIAL_PORT").ok().filter(|s| !s.is_empty()),
                serial_baud: get_env_u32("WAKE_SERIAL_BAUD", 115_200).max(1_200),
                serial_timeout_secs: get_env_f32("WAKE_SERIAL_TIMEOUT", 0.2).max(0.05),
                serial_reconnect_secs: get_env_f32("WAKE_SERIAL_RECONNECT_SEC", 1.0).max(0.2),
                debounce_secs: get_env_f32("WAKE_DEBOUNCE_SEC", 0.5).max(0.0),
                http_enabled: get_env_bool("WAKE_HTTP_ENABLED", true),
                http_host: get_env_non_empty("WAKE_HTTP_HOST", "127.0.0.1"),
                http_port: get_env_u16("WAKE_HTTP_PORT", 5_202).max(1),
                http_path: normalize_http_path(&get_env_non_empty("WAKE_HTTP_PATH", "/wakeup")),
                kws_host: get_env_non_empty("KWS_WAKE_HOST", "127.0.0.1"),
                kws_port: get_env_u16("KWS_WAKE_PORT", 39_004).max(1),
                kws_reconnect_secs: get_env_f32("KWS_WAKE_RECONNECT_SEC", 1.0).max(0.1),
                angle_report_url: env::var("ANGLE_REPORT_URL")
                    .unwrap_or_else(|_| "http://127.0.0.1:19992/api/robot/angle".to_string()),
                angle_report_timeout_secs: get_env_f32("ANGLE_REPORT_TIMEOUT", 1.0).max(0.1),
            },
        })
    }

    pub fn printable(&self) -> PrintableConfig {
        PrintableConfig {
            gateway: PrintableGatewayConfig {
                url: self.gateway.url.clone(),
                bot_id: self.gateway.bot_id.clone(),
                robot_id: self.gateway.robot_id.clone(),
                robot_secret: secret_status(self.gateway.robot_secret.as_deref()),
                heartbeat_interval_secs: self.gateway.heartbeat_interval_secs,
                connect_timeout_secs: self.gateway.connect_timeout_secs,
                send_timeout_secs: self.gateway.send_timeout_secs,
            },
            audio: self.audio.clone(),
            audio_control: self.audio_control.clone(),
            environment_trigger: self.environment_trigger.clone(),
            feedback: self.feedback.clone(),
            vad: self.vad.clone(),
            turn_gate_shadow: self.turn_gate_shadow.clone(),
            tts: self.tts.clone(),
            transport: PrintableTransportConfig {
                policy: self.transport.policy.as_str(),
                webrtc_enabled: self.transport.webrtc_enabled,
                webrtc_connect_timeout_ms: self.transport.webrtc_connect_timeout_ms,
                fallback_enabled: self.transport.fallback_enabled,
                webrtc_audio_uplink_mode: self.transport.webrtc_audio_uplink_mode.as_str(),
            },
            vision: self.vision.clone(),
            runtime: self.runtime.clone(),
            wake: self.wake.clone(),
        }
    }
}

fn secret_status(value: Option<&str>) -> Option<&'static str> {
    value
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(|_| "<set>")
}

fn resolve_first_existing_path(candidates: &[&str]) -> Option<String> {
    let mut roots: Vec<PathBuf> = Vec::new();

    if let Ok(current_dir) = env::current_dir() {
        roots.push(current_dir);
    }
    roots.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")));

    for root in roots {
        for candidate in candidates {
            let resolved = resolve_path(&root, candidate);
            if resolved.exists() {
                return Some(resolved.to_string_lossy().into_owned());
            }
        }
    }

    None
}

fn resolve_path(root: &Path, candidate: &str) -> PathBuf {
    let path = PathBuf::from(candidate);
    if path.is_absolute() {
        path
    } else {
        root.join(path)
    }
}

fn build_gateway_url() -> Result<String> {
    if let Ok(url) = env::var("GATEWAY_URL") {
        let url = url.trim();
        if !url.is_empty() {
            ensure!(
                url.starts_with("ws://") || url.starts_with("wss://"),
                "GATEWAY_URL 必须以 ws:// 或 wss:// 开头"
            );
            return Ok(url.to_string());
        }
    }

    let scheme = env::var("GATEWAY_SCHEME")
        .unwrap_or_else(|_| DEFAULT_GATEWAY_SCHEME.to_string())
        .trim()
        .to_lowercase();
    ensure!(
        scheme == "ws" || scheme == "wss",
        "GATEWAY_SCHEME 只能是 ws 或 wss"
    );

    let host = env::var("GATEWAY_HOST")
        .unwrap_or_else(|_| DEFAULT_GATEWAY_HOST.to_string())
        .trim()
        .to_string();
    ensure!(!host.is_empty(), "GATEWAY_HOST 不能为空");

    let port = get_env_u32("GATEWAY_PORT", DEFAULT_GATEWAY_PORT).max(1);
    let path = env::var("GATEWAY_PATH")
        .unwrap_or_else(|_| DEFAULT_GATEWAY_PATH.to_string())
        .trim()
        .to_string();
    let path = if path.starts_with('/') {
        path
    } else {
        format!("/{path}")
    };

    Ok(format!("{scheme}://{host}:{port}{path}"))
}

fn get_optional_path_env(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn get_env_f32(name: &str, default: f32) -> f32 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<f32>().ok())
        .unwrap_or(default)
}

fn get_env_u64(name: &str, default: u64) -> u64 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(default)
}

fn get_env_u32(name: &str, default: u32) -> u32 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<u32>().ok())
        .unwrap_or(default)
}

fn get_env_u16(name: &str, default: u16) -> u16 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(default)
}

fn get_env_usize(name: &str, default: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(default)
}

fn get_env_u8(name: &str, default: u8) -> u8 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<u8>().ok())
        .unwrap_or(default)
}

fn get_env_non_empty(name: &str, default: &str) -> String {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn normalize_http_path(path: &str) -> String {
    let path = path.trim();
    if path.is_empty() {
        "/".to_string()
    } else if path.starts_with('/') {
        path.to_string()
    } else {
        format!("/{path}")
    }
}

fn get_env_bool(name: &str, default: bool) -> bool {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_ascii_lowercase())
        .and_then(|value| match value.as_str() {
            "1" | "true" | "yes" | "y" | "on" => Some(true),
            "0" | "false" | "no" | "n" | "off" => Some(false),
            _ => None,
        })
        .unwrap_or(default)
}

trait IntoNonEmpty {
    fn into_non_empty(self) -> Option<String>;
}

impl IntoNonEmpty for String {
    fn into_non_empty(self) -> Option<String> {
        if self.trim().is_empty() {
            None
        } else {
            Some(self)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Mutex, MutexGuard, OnceLock};

    static ENV_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

    struct SavedEnv {
        values: Vec<(&'static str, Option<String>)>,
    }

    impl SavedEnv {
        fn clear(names: &[&'static str]) -> Self {
            let values = names
                .iter()
                .map(|name| {
                    let value = env::var(name).ok();
                    env::remove_var(name);
                    (*name, value)
                })
                .collect();
            Self { values }
        }
    }

    impl Drop for SavedEnv {
        fn drop(&mut self) {
            for (name, value) in &self.values {
                match value {
                    Some(value) => env::set_var(name, value),
                    None => env::remove_var(name),
                }
            }
        }
    }

    fn env_guard() -> MutexGuard<'static, ()> {
        ENV_LOCK
            .get_or_init(|| Mutex::new(()))
            .lock()
            .expect("env test lock poisoned")
    }

    #[test]
    fn default_gateway_url_points_to_v3_go_gateway() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "GATEWAY_URL",
            "GATEWAY_SCHEME",
            "GATEWAY_HOST",
            "GATEWAY_PORT",
            "GATEWAY_PATH",
            "WAKE_SOURCE_MODE",
            "KWS_WAKE_HOST",
            "KWS_WAKE_PORT",
            "KWS_WAKE_RECONNECT_SEC",
        ]);

        let url = build_gateway_url().expect("default gateway URL should be valid");

        assert_eq!(url, "ws://127.0.0.1:8282/ws");
    }

    #[test]
    fn default_transport_uses_v3_webrtc_only_path() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "ROBOT_ID",
            "GATEWAY_URL",
            "GATEWAY_SCHEME",
            "GATEWAY_HOST",
            "GATEWAY_PORT",
            "GATEWAY_PATH",
            "TRANSPORT_POLICY",
            "WEBRTC_ENABLED",
            "WEBRTC_CONNECT_TIMEOUT_MS",
            "TRANSPORT_FALLBACK_ENABLED",
            "RTC_AUDIO_UPLINK",
            "VISION_SNAPSHOT_ENABLED",
            "VISION_SNAPSHOT_PATH",
            "VISION_SNAPSHOT_SCAN_INTERVAL_MS",
            "AUDIO_FRONTEND_MODE",
            "AUDIO_FRONTEND_HOST",
            "AUDIO_FRONTEND_MIC_PORT",
            "AUDIO_FRONTEND_SPEAKER_PORT",
            "AUDIO_FRONTEND_CONTROL_PORT",
            "AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK",
            "AUDIO_FRONTEND_UPSAMPLER",
            "TTS_BARGE_IN_WITH_AEC",
            "NATURAL_BARGE_IN_ENABLED",
            "WAKE_CAPTURE_PREBUFFER_MS",
            "WAKE_SOURCE_MODE",
            "KWS_WAKE_HOST",
            "KWS_WAKE_PORT",
            "KWS_WAKE_RECONNECT_SEC",
        ]);
        env::set_var("ROBOT_ID", "test_01");

        let cfg = Config::from_env().expect("config should load with default v3 transport");

        assert_eq!(cfg.gateway.url, "ws://127.0.0.1:8282/ws");
        assert_eq!(cfg.transport.policy, TransportPolicy::WebRtcOnly);
        assert!(cfg.transport.webrtc_enabled);
        assert_eq!(cfg.transport.webrtc_connect_timeout_ms, 15_000);
        assert!(!cfg.transport.fallback_enabled);
        assert_eq!(cfg.runtime.response_timeout_secs, 45);
        assert_eq!(
            cfg.transport.webrtc_audio_uplink_mode,
            WebRtcAudioUplinkMode::RtpOnly
        );
        assert!(!cfg.vision.enabled);
        assert_eq!(cfg.vision.snapshot_path, "temp/pic.jpeg");
        assert_eq!(cfg.vision.scan_interval_ms, 3000);
        assert_eq!(cfg.audio.frontend_mode, AudioFrontendMode::Cpal);
        assert_eq!(cfg.audio.frontend_host, "127.0.0.1");
        assert_eq!(cfg.audio.frontend_mic_port, 39_001);
        assert_eq!(cfg.audio.frontend_speaker_port, 39_002);
        assert_eq!(cfg.audio.frontend_control_port, 39_003);
        assert!(!cfg.audio.frontend_mute_mic_during_playback);
        assert_eq!(cfg.audio.frontend_upsampler, AudioFrontendUpsampler::Repeat);
        assert!(!cfg.audio.tts_barge_in_with_aec);
        assert!(!cfg.audio.should_detect_tts_barge_in());
        assert!(!cfg.audio.natural_barge_in_enabled);
        assert!(!cfg.audio.should_detect_natural_barge_in());
        assert!(cfg.audio.should_pause_capture_while_waiting());
        assert_eq!(cfg.audio.wake_capture_prebuffer_ms, 800);
        assert_eq!(cfg.wake.source_mode, WakeSourceMode::Hardware);
        assert_eq!(cfg.wake.kws_host, "127.0.0.1");
        assert_eq!(cfg.wake.kws_port, 39_004);
        assert_eq!(cfg.wake.kws_reconnect_secs, 1.0);
    }

    #[test]
    fn default_vad_matches_production_baseline() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "ROBOT_ID",
            "VAD_MODE",
            "VAD_SPEECH_START_MIN_DBFS",
            "VAD_SILENCE_THRESHOLD",
            "VAD_SPEECH_THRESHOLD",
            "VAD_SPEECH_TRIGGER_RATIO",
            "VAD_MAX_RECORDING_DURATION",
            "TURN_GATE_SHADOW_ENABLED",
            "TURN_GATE_ACTIVE_ENABLED",
            "TURN_GATE_CANDIDATE_SILENCE_MS",
            "TURN_GATE_MIN_COMMIT_SILENCE_MS",
            "TURN_GATE_FAILURE_FALLBACK_MS",
            "TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS",
        ]);
        env::set_var("ROBOT_ID", "test_01");

        let cfg = Config::from_env().expect("config should load default VAD baseline");

        assert_eq!(cfg.vad.mode, 2);
        assert_eq!(cfg.vad.speech_start_min_dbfs, -36.0);
        assert_eq!(cfg.vad.silence_threshold_secs, 0.5);
        assert_eq!(cfg.vad.speech_threshold_secs, 0.25);
        assert_eq!(cfg.vad.speech_trigger_ratio, 0.6);
        assert_eq!(cfg.vad.max_recording_duration_secs, 10);
        assert!(cfg.turn_gate_shadow.enabled);
        assert!(cfg.turn_gate_shadow.active_enabled);
        assert_eq!(cfg.turn_gate_shadow.candidate_silence_ms, 300);
        assert_eq!(cfg.turn_gate_shadow.min_commit_silence_ms, 600);
        assert_eq!(cfg.turn_gate_shadow.failure_fallback_ms, 1000);
        assert_eq!(cfg.turn_gate_shadow.ambiguous_hard_timeout_ms, 1600);
    }

    #[test]
    fn active_turn_gate_can_be_explicitly_disabled() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "ROBOT_ID",
            "TURN_GATE_SHADOW_ENABLED",
            "TURN_GATE_ACTIVE_ENABLED",
        ]);
        env::set_var("ROBOT_ID", "test_01");
        env::set_var("TURN_GATE_ACTIVE_ENABLED", "false");

        let cfg = Config::from_env().expect("config should support active rollback");

        assert!(!cfg.turn_gate_shadow.enabled);
        assert!(!cfg.turn_gate_shadow.active_enabled);
    }

    #[test]
    fn active_turn_gate_timeouts_follow_candidate_order() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "ROBOT_ID",
            "TURN_GATE_ACTIVE_ENABLED",
            "TURN_GATE_CANDIDATE_SILENCE_MS",
            "TURN_GATE_MIN_COMMIT_SILENCE_MS",
            "TURN_GATE_FAILURE_FALLBACK_MS",
            "TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS",
        ]);
        env::set_var("ROBOT_ID", "test_01");
        env::set_var("TURN_GATE_ACTIVE_ENABLED", "true");
        env::set_var("TURN_GATE_CANDIDATE_SILENCE_MS", "400");
        env::set_var("TURN_GATE_MIN_COMMIT_SILENCE_MS", "300");
        env::set_var("TURN_GATE_FAILURE_FALLBACK_MS", "300");
        env::set_var("TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS", "200");

        let cfg = Config::from_env().expect("config should clamp active timing order");

        assert!(cfg.turn_gate_shadow.enabled);
        assert!(cfg.turn_gate_shadow.active_enabled);
        assert_eq!(cfg.turn_gate_shadow.candidate_silence_ms, 400);
        assert_eq!(cfg.turn_gate_shadow.min_commit_silence_ms, 400);
        assert_eq!(cfg.turn_gate_shadow.failure_fallback_ms, 400);
        assert_eq!(cfg.turn_gate_shadow.ambiguous_hard_timeout_ms, 400);
    }

    #[test]
    fn audio_frontend_tcp_env_is_parsed() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "ROBOT_ID",
            "AUDIO_FRONTEND_MODE",
            "AUDIO_FRONTEND_HOST",
            "AUDIO_FRONTEND_MIC_PORT",
            "AUDIO_FRONTEND_SPEAKER_PORT",
            "AUDIO_FRONTEND_CONTROL_PORT",
            "AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK",
            "AUDIO_FRONTEND_UPSAMPLER",
            "TTS_BARGE_IN_WITH_AEC",
            "WAKE_CAPTURE_PREBUFFER_MS",
        ]);
        env::set_var("ROBOT_ID", "test_01");
        env::set_var("AUDIO_FRONTEND_MODE", "tcp");
        env::set_var("AUDIO_FRONTEND_HOST", "127.0.0.2");
        env::set_var("AUDIO_FRONTEND_MIC_PORT", "49001");
        env::set_var("AUDIO_FRONTEND_SPEAKER_PORT", "49002");
        env::set_var("AUDIO_FRONTEND_CONTROL_PORT", "49003");
        env::set_var("AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK", "true");
        env::set_var("AUDIO_FRONTEND_UPSAMPLER", "linear");
        env::set_var("TTS_BARGE_IN_WITH_AEC", "true");
        env::set_var("WAKE_CAPTURE_PREBUFFER_MS", "1200");

        let cfg = Config::from_env().expect("config should load");

        assert_eq!(cfg.audio.frontend_mode, AudioFrontendMode::Tcp);
        assert_eq!(cfg.audio.frontend_host, "127.0.0.2");
        assert_eq!(cfg.audio.frontend_mic_port, 49_001);
        assert_eq!(cfg.audio.frontend_speaker_port, 49_002);
        assert_eq!(cfg.audio.frontend_control_port, 49_003);
        assert!(cfg.audio.frontend_mute_mic_during_playback);
        assert_eq!(cfg.audio.frontend_upsampler, AudioFrontendUpsampler::Linear);
        assert!(cfg.audio.tts_barge_in_with_aec);
        assert!(!cfg.audio.should_detect_tts_barge_in());
        assert!(cfg.audio.should_pause_capture_while_waiting());
        assert_eq!(cfg.audio.wake_capture_prebuffer_ms, 1200);
    }

    #[test]
    fn tts_barge_in_with_aec_is_opt_in_in_tcp_mode() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "ROBOT_ID",
            "AUDIO_FRONTEND_MODE",
            "AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK",
            "TTS_BARGE_IN_WITH_AEC",
        ]);
        env::set_var("ROBOT_ID", "test_01");
        env::set_var("AUDIO_FRONTEND_MODE", "tcp");
        env::set_var("AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK", "false");

        let cfg = Config::from_env().expect("config should load");
        assert!(!cfg.audio.tts_barge_in_with_aec);
        assert!(!cfg.audio.should_detect_tts_barge_in());

        env::set_var("TTS_BARGE_IN_WITH_AEC", "true");
        let cfg = Config::from_env().expect("config should load");
        assert!(cfg.audio.tts_barge_in_with_aec);
        assert!(cfg.audio.should_detect_tts_barge_in());

        env::set_var("TTS_BARGE_IN_WITH_AEC", "false");
        let cfg = Config::from_env().expect("config should load");
        assert!(!cfg.audio.tts_barge_in_with_aec);
        assert!(!cfg.audio.should_detect_tts_barge_in());
    }

    #[test]
    fn natural_barge_in_is_opt_in_for_system_audio_and_requires_unmuted_mic() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "ROBOT_ID",
            "AUDIO_FRONTEND_MODE",
            "AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK",
            "NATURAL_BARGE_IN_ENABLED",
        ]);
        env::set_var("ROBOT_ID", "test_01");
        env::set_var("AUDIO_FRONTEND_MODE", "cpal");

        let cfg = Config::from_env().expect("config should load");
        assert!(!cfg.audio.should_detect_natural_barge_in());

        env::set_var("NATURAL_BARGE_IN_ENABLED", "true");
        let cfg = Config::from_env().expect("config should load");
        assert!(cfg.audio.should_detect_natural_barge_in());

        env::set_var("AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK", "true");
        let cfg = Config::from_env().expect("config should load");
        assert!(!cfg.audio.should_detect_natural_barge_in());
    }

    #[test]
    fn wake_source_mode_and_kws_tcp_config_are_parsed() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "ROBOT_ID",
            "WAKE_SOURCE_MODE",
            "KWS_WAKE_HOST",
            "KWS_WAKE_PORT",
            "KWS_WAKE_RECONNECT_SEC",
        ]);
        env::set_var("ROBOT_ID", "test_01");
        env::set_var("WAKE_SOURCE_MODE", "both");
        env::set_var("KWS_WAKE_HOST", "127.0.0.2");
        env::set_var("KWS_WAKE_PORT", "39104");
        env::set_var("KWS_WAKE_RECONNECT_SEC", "0.25");

        let cfg = Config::from_env().expect("config should load KWS wake settings");

        assert_eq!(cfg.wake.source_mode, WakeSourceMode::Both);
        assert!(cfg.wake.source_mode.hardware_enabled());
        assert!(cfg.wake.source_mode.kws_enabled());
        assert_eq!(cfg.wake.kws_host, "127.0.0.2");
        assert_eq!(cfg.wake.kws_port, 39_104);
        assert_eq!(cfg.wake.kws_reconnect_secs, 0.25);
    }

    #[test]
    fn printable_config_redacts_robot_secret() {
        let _guard = env_guard();
        let _saved = SavedEnv::clear(&[
            "ROBOT_ID",
            "ROBOT_SECRET",
            "GATEWAY_URL",
            "GATEWAY_SCHEME",
            "GATEWAY_HOST",
            "GATEWAY_PORT",
            "GATEWAY_PATH",
        ]);
        env::set_var("ROBOT_ID", "test_01");
        env::set_var("ROBOT_SECRET", "secret-value");

        let cfg = Config::from_env().expect("config should load");
        let printable = cfg.printable();
        let payload = serde_json::to_string(&printable).expect("printable config should serialize");

        assert!(!payload.contains("secret-value"));
        assert!(payload.contains(r#""robot_secret":"<set>""#));
        assert!(payload.contains(r#""url":"ws://127.0.0.1:8282/ws""#));
    }

    #[test]
    fn env_help_text_includes_key_mappings() {
        let text = env_help_text();

        for expected in [
            "gateway.url",
            "GATEWAY_URL",
            "gateway.robot_id",
            "ROBOT_ID",
            "transport.policy",
            "TRANSPORT_POLICY",
            "vision.snapshot_path",
            "VISION_SNAPSHOT_PATH",
            "audio.frontend_mode",
            "AUDIO_FRONTEND_MODE",
            "audio.frontend_control_port",
            "AUDIO_FRONTEND_CONTROL_PORT",
            "audio.frontend_mute_mic_during_playback",
            "AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK",
            "audio.frontend_upsampler",
            "AUDIO_FRONTEND_UPSAMPLER",
            "audio.wake_capture_prebuffer_ms",
            "WAKE_CAPTURE_PREBUFFER_MS",
            "turn_gate_shadow.min_commit_silence_ms",
            "TURN_GATE_MIN_COMMIT_SILENCE_MS",
            "tts.prebuffer_secs",
            "TTS_PREBUFFER_SEC",
            "webrtc.native_ice_network_types",
            "WEBRTC_NATIVE_ICE_NETWORK_TYPES",
            "wake.source_mode",
            "WAKE_SOURCE_MODE",
            "wake.kws_port",
            "KWS_WAKE_PORT",
        ] {
            assert!(
                text.contains(expected),
                "env help missing {expected:?}:\n{text}"
            );
        }
    }
}
