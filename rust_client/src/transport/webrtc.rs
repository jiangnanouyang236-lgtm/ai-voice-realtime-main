use super::{TransportEvent, TransportKind, VoiceTransport, WebRtcAudioUplinkMode};
#[cfg(feature = "native-webrtc")]
use crate::audio::opus_codec::{
    parse_opus_packet_stream, OPUS_CHANNELS, OPUS_FRAME_DURATION_MS, OPUS_PACKET_STREAM_MAGIC,
    OPUS_SAMPLE_RATE,
};
#[cfg(feature = "native-webrtc")]
use crate::protocol::{AudioFrame, AUDIO_FRAME_VERSION};
#[cfg(feature = "native-webrtc")]
use crate::snapshot::{
    consume_snapshot, encode_vision_chunks, parse_vision_ack, SnapshotPolicy,
    VISION_DATA_CHANNEL_LABEL,
};
use crate::{
    config::VisionConfig,
    gateway::{GatewayConnection, GatewayEvent},
    protocol::{
        rtc_signaling::{
            IceServer, RtcAnswer, RtcConfig, RtcIceCandidate, RtcOffer, TransportFallbackAck,
            TransportFallbackStart, TransportReady,
        },
        AudioFrameHeader, ClientMessage, ServerMessage,
    },
};
#[cfg(feature = "native-webrtc")]
use anyhow::Context;
use anyhow::{anyhow, ensure, Result};
#[cfg(feature = "native-webrtc")]
use bytes::Bytes;
use futures_util::future::BoxFuture;
#[cfg(feature = "native-webrtc")]
use rtp::{header::Header as RtpHeader, packet::Packet as RtpPacket};
#[cfg(feature = "native-webrtc")]
use std::collections::HashMap;
#[cfg(feature = "native-webrtc")]
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
#[cfg(feature = "native-webrtc")]
use std::time::{SystemTime, UNIX_EPOCH};
use std::{
    env,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{process::Command, sync::mpsc, time};
use tracing::{debug, info, warn};
#[cfg(feature = "native-webrtc")]
use webrtc::{
    api::{
        media_engine::{MediaEngine, MIME_TYPE_OPUS},
        setting_engine::SettingEngine,
        APIBuilder,
    },
    data_channel::{
        data_channel_init::RTCDataChannelInit, data_channel_message::DataChannelMessage,
        data_channel_state::RTCDataChannelState, RTCDataChannel,
    },
    ice_transport::{
        ice_candidate::RTCIceCandidateInit, ice_candidate_pair::RTCIceCandidatePair,
        ice_candidate_type::RTCIceCandidateType, ice_gatherer_state::RTCIceGathererState,
        ice_server::RTCIceServer,
    },
    peer_connection::{
        configuration::RTCConfiguration, peer_connection_state::RTCPeerConnectionState,
        sdp::session_description::RTCSessionDescription, RTCPeerConnection,
    },
    rtp_transceiver::rtp_codec::RTCRtpCodecCapability,
    track::{
        track_local::{track_local_static_rtp::TrackLocalStaticRTP, TrackLocal, TrackLocalWriter},
        track_remote::TrackRemote,
    },
};
#[cfg(feature = "native-webrtc")]
use webrtc_ice::network_type::NetworkType;

const FALLBACK_FROM_TRANSPORT: &str = "webrtc_rtp";
const FALLBACK_TO_TRANSPORT: &str = "websocket";
const FALLBACK_REASON_OFFER_UNAVAILABLE: &str = "client_webrtc_offer_not_available";
const WEBRTC_OFFER_HELPER_ENV: &str = "WEBRTC_OFFER_HELPER";
const WEBRTC_OFFER_HELPER_TIMEOUT_MS_ENV: &str = "WEBRTC_OFFER_HELPER_TIMEOUT_MS";
const WEBRTC_OFFER_FACTORY_ENV: &str = "WEBRTC_OFFER_FACTORY";
const WEBRTC_NATIVE_ENABLED_ENV: &str = "WEBRTC_NATIVE_WEBRTC_ENABLED";
#[cfg(feature = "native-webrtc")]
const WEBRTC_NATIVE_GATHER_TIMEOUT_MS_ENV: &str = "WEBRTC_NATIVE_GATHER_TIMEOUT_MS";
#[cfg(feature = "native-webrtc")]
const WEBRTC_NATIVE_ICE_NETWORK_TYPES_ENV: &str = "WEBRTC_NATIVE_ICE_NETWORK_TYPES";
#[cfg(feature = "native-webrtc")]
const WEBRTC_NATIVE_RTP_PROBE_ENABLED_ENV: &str = "WEBRTC_NATIVE_RTP_PROBE_ENABLED";
const DEFAULT_OFFER_HELPER_TIMEOUT_MS: u64 = 5_000;
#[cfg(feature = "native-webrtc")]
const DEFAULT_NATIVE_GATHER_TIMEOUT_MS: u64 = 5_000;
#[cfg(feature = "native-webrtc")]
const DEFAULT_NATIVE_ICE_NETWORK_TYPES: &str = "udp4";
#[cfg(feature = "native-webrtc")]
const NATIVE_DATA_CHANNEL_LABEL: &str = "control";
#[cfg(feature = "native-webrtc")]
const NATIVE_VISION_MAX_BUFFERED_BYTES: usize = 128 * 1024;
#[cfg(feature = "native-webrtc")]
const NATIVE_VISION_MAX_CAPTURE_AGE_MS: i64 = 5_000;
#[cfg(feature = "native-webrtc")]
static NEXT_VISION_FRAME_ID: AtomicU64 = AtomicU64::new(1);
#[cfg(feature = "native-webrtc")]
const NATIVE_AUDIO_TRACK_ID: &str = "microphone";
#[cfg(feature = "native-webrtc")]
const NATIVE_AUDIO_STREAM_ID: &str = "voice";
#[cfg(feature = "native-webrtc")]
const NATIVE_RTP_PROBE_PACKET_COUNT: u16 = 3;
#[cfg(feature = "native-webrtc")]
const NATIVE_OPUS_RTP_PAYLOAD_TYPE: u8 = 111;
#[cfg(feature = "native-webrtc")]
const NATIVE_OPUS_RTP_SSRC: u32 = 0x575a_4b01;
#[cfg(feature = "native-webrtc")]
const NATIVE_OPUS_RTP_TIMESTAMP_STEP: u32 = 960;
#[cfg(feature = "native-webrtc")]
const NATIVE_OPUS_RTP_PROBE_PAYLOAD: &[u8] = &[0xf8, 0xff, 0xfe];

pub struct WebRtcConnectOptions<'a> {
    pub signaling_url: &'a str,
    pub connect_timeout: Duration,
    pub write_timeout: Duration,
    pub audio_uplink_mode: WebRtcAudioUplinkMode,
    pub vision: &'a VisionConfig,
}

#[derive(Clone)]
#[allow(dead_code)]
pub struct WebRtcVoiceTransport {
    connection: GatewayConnection,
    kind: TransportKind,
    signaling_state: SharedSignalingState,
    offer_factory: SharedOfferFactory,
    audio_uplink_mode: WebRtcAudioUplinkMode,
}

type SharedSignalingState = Arc<Mutex<WebRtcSignalingState>>;
type SharedOfferFactory = Arc<dyn RtcOfferFactory>;

#[derive(Debug)]
struct ServerSignalingUpdate {
    updated: bool,
    offer_request: Option<RtcConfig>,
}

struct OfferFactoryBundle {
    factory: SharedOfferFactory,
    client_actions: Option<mpsc::Receiver<ClientMessage>>,
    native_events: Option<mpsc::Receiver<TransportEvent>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code)]
pub enum RtcNegotiationAction {
    SendOffer(RtcOffer),
    StartFallback(TransportFallbackStart),
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code)]
pub struct RtcPreflightReport {
    pub session_id: String,
    pub ice_server_count: usize,
    pub stun_url_count: usize,
    pub turn_url_count: usize,
    pub empty_url_count: usize,
    pub unknown_url_count: usize,
    pub turn_servers_missing_credentials: usize,
    pub audio_codec: String,
    pub audio_sample_rate: u32,
    pub audio_channels: u16,
    pub audio_ptime_ms: u16,
    pub warnings: Vec<String>,
}

impl RtcPreflightReport {
    pub fn from_config(config: &RtcConfig) -> Self {
        let mut report = Self {
            session_id: config.session_id.clone(),
            ice_server_count: config.ice_servers.len(),
            stun_url_count: 0,
            turn_url_count: 0,
            empty_url_count: 0,
            unknown_url_count: 0,
            turn_servers_missing_credentials: 0,
            audio_codec: config.media.audio.codec.clone(),
            audio_sample_rate: config.media.audio.sample_rate,
            audio_channels: config.media.audio.channels,
            audio_ptime_ms: config.media.audio.ptime_ms,
            warnings: Vec::new(),
        };

        for server in &config.ice_servers {
            report.add_ice_server(server);
        }
        report.add_audio_warnings();
        report.add_ice_warnings();
        report
    }

    #[allow(dead_code)]
    pub fn has_relay(&self) -> bool {
        self.turn_url_count > 0
    }

    #[allow(dead_code)]
    pub fn has_stun(&self) -> bool {
        self.stun_url_count > 0
    }

    pub fn warning_summary(&self) -> String {
        if self.warnings.is_empty() {
            "-".to_string()
        } else {
            self.warnings.join(",")
        }
    }

    fn add_ice_server(&mut self, server: &IceServer) {
        let mut server_has_turn = false;
        for url in &server.urls {
            let normalized = url.trim().to_ascii_lowercase();
            if normalized.is_empty() {
                self.empty_url_count += 1;
            } else if normalized.starts_with("stun:") || normalized.starts_with("stuns:") {
                self.stun_url_count += 1;
            } else if normalized.starts_with("turn:") || normalized.starts_with("turns:") {
                self.turn_url_count += 1;
                server_has_turn = true;
            } else {
                self.unknown_url_count += 1;
            }
        }

        if server_has_turn
            && (is_blank_option(server.username.as_deref())
                || is_blank_option(server.credential.as_deref()))
        {
            self.turn_servers_missing_credentials += 1;
        }
    }

    fn add_ice_warnings(&mut self) {
        if self.ice_server_count == 0 {
            self.warnings.push("ice_servers_empty".to_string());
        }
        if self.stun_url_count == 0 && self.turn_url_count == 0 {
            self.warnings.push("no_stun_or_turn_url".to_string());
        }
        if self.empty_url_count > 0 {
            self.warnings.push("empty_ice_url".to_string());
        }
        if self.unknown_url_count > 0 {
            self.warnings.push("unknown_ice_url_scheme".to_string());
        }
        if self.turn_servers_missing_credentials > 0 {
            self.warnings
                .push("turn_missing_username_or_credential".to_string());
        }
    }

    fn add_audio_warnings(&mut self) {
        if !self.audio_codec.eq_ignore_ascii_case("opus") {
            self.warnings.push("audio_codec_not_opus".to_string());
        }
        if self.audio_sample_rate != 48_000 {
            self.warnings
                .push("audio_sample_rate_not_webrtc_opus_clock".to_string());
        }
        if self.audio_channels == 0 || self.audio_channels > 2 {
            self.warnings.push("audio_channels_unsupported".to_string());
        }
        if !matches!(self.audio_ptime_ms, 10 | 20 | 40 | 60) {
            self.warnings.push("audio_ptime_unusual".to_string());
        }
    }
}

pub trait RtcOfferFactory: Send + Sync {
    fn create_offer<'a>(
        &'a self,
        config: &'a RtcConfig,
    ) -> BoxFuture<'a, Result<RtcNegotiationAction>>;

    fn send_audio_frame<'a>(
        &'a self,
        _header: &'a AudioFrameHeader,
        _payload: &'a [u8],
    ) -> BoxFuture<'a, Result<bool>> {
        Box::pin(async { Ok(false) })
    }

    fn send_control<'a>(&'a self, _message: &'a ClientMessage) -> BoxFuture<'a, Result<bool>> {
        Box::pin(async { Ok(false) })
    }

    fn audio_uplink_ready(&self) -> bool {
        false
    }

    fn apply_answer<'a>(&'a self, _answer: &'a RtcAnswer) -> BoxFuture<'a, Result<()>> {
        Box::pin(async { Ok(()) })
    }

    fn add_remote_candidate<'a>(
        &'a self,
        _candidate: &'a RtcIceCandidate,
    ) -> BoxFuture<'a, Result<()>> {
        Box::pin(async { Ok(()) })
    }

    fn drain_local_candidates(&self, _session_id: &str) -> Result<Vec<RtcIceCandidate>> {
        Ok(Vec::new())
    }

    fn close_sessions(&self, _reason: &'static str) {}
}

#[derive(Debug, Clone, Copy, Default)]
struct FallbackOnlyRtcOfferFactory;

impl RtcOfferFactory for FallbackOnlyRtcOfferFactory {
    fn create_offer<'a>(
        &'a self,
        config: &'a RtcConfig,
    ) -> BoxFuture<'a, Result<RtcNegotiationAction>> {
        Box::pin(async move {
            Ok(RtcNegotiationAction::StartFallback(
                fallback_start_for_config(config),
            ))
        })
    }
}

#[derive(Debug, Clone)]
struct HelperProcessRtcOfferFactory {
    command: String,
    timeout: Duration,
}

impl HelperProcessRtcOfferFactory {
    fn from_env() -> Option<Self> {
        let command = env::var(WEBRTC_OFFER_HELPER_ENV)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())?;
        let timeout_ms = env::var(WEBRTC_OFFER_HELPER_TIMEOUT_MS_ENV)
            .ok()
            .and_then(|value| value.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_OFFER_HELPER_TIMEOUT_MS)
            .max(1);
        Some(Self {
            command,
            timeout: Duration::from_millis(timeout_ms),
        })
    }

    async fn run_helper(&self, config: &RtcConfig) -> Result<RtcOffer> {
        let request = serde_json::to_vec(config)?;
        let mut child = Command::new("sh")
            .arg("-c")
            .arg(&self.command)
            .kill_on_drop(true)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .map_err(|err| anyhow!("启动 WebRTC offer helper 失败: {err}"))?;

        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("WebRTC offer helper stdin 不可用"))?;
        tokio::spawn(async move {
            use tokio::io::AsyncWriteExt;
            if stdin.write_all(&request).await.is_ok() {
                let _ = stdin.shutdown().await;
            }
        });

        let output = time::timeout(self.timeout, child.wait_with_output())
            .await
            .map_err(|_| anyhow!("WebRTC offer helper 超时: {}ms", self.timeout.as_millis()))?
            .map_err(|err| anyhow!("WebRTC offer helper 执行失败: {err}"))?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(anyhow!(
                "WebRTC offer helper 退出失败: status={} stderr={}",
                output.status,
                stderr.trim()
            ));
        }

        let offer: RtcOffer = serde_json::from_slice(&output.stdout)
            .map_err(|err| anyhow!("解析 WebRTC offer helper stdout 失败: {err}"))?;
        ensure!(
            offer.session_id == config.session_id,
            "WebRTC offer helper session_id 不匹配: expected={}, actual={}",
            config.session_id,
            offer.session_id
        );
        ensure!(
            !offer.sdp.trim().is_empty(),
            "WebRTC offer helper 返回空 SDP"
        );
        Ok(offer)
    }
}

impl RtcOfferFactory for HelperProcessRtcOfferFactory {
    fn create_offer<'a>(
        &'a self,
        config: &'a RtcConfig,
    ) -> BoxFuture<'a, Result<RtcNegotiationAction>> {
        Box::pin(async move {
            let offer = self.run_helper(config).await?;
            Ok(RtcNegotiationAction::SendOffer(offer))
        })
    }
}

#[cfg(feature = "native-webrtc")]
struct NativeWebRtcOfferFactory {
    sessions: Mutex<HashMap<String, NativePeerSession>>,
    gather_timeout: Duration,
    client_action_tx: mpsc::Sender<ClientMessage>,
    native_event_tx: mpsc::Sender<TransportEvent>,
    vision: VisionConfig,
}

#[cfg(feature = "native-webrtc")]
struct NativePeerSession {
    peer_connection: Arc<RTCPeerConnection>,
    data_channel: Arc<RTCDataChannel>,
    _vision_data_channel: Option<Arc<RTCDataChannel>>,
    control_open: Arc<AtomicBool>,
    alive: Arc<AtomicBool>,
    audio_track: Arc<TrackLocalStaticRTP>,
    rtp_state: Arc<Mutex<NativeRtpSendState>>,
    local_candidates: Arc<Mutex<Vec<RtcIceCandidate>>>,
}

#[cfg(feature = "native-webrtc")]
struct NativeControlSender {
    session_id: String,
    data_channel: Arc<RTCDataChannel>,
}

#[cfg(feature = "native-webrtc")]
#[derive(Debug)]
struct NativeAudioSender {
    session_id: String,
    audio_track: Arc<TrackLocalStaticRTP>,
    rtp_state: Arc<Mutex<NativeRtpSendState>>,
}

#[cfg(feature = "native-webrtc")]
#[derive(Debug)]
struct NativeRtpSendState {
    next_sequence_number: u16,
    next_timestamp: u32,
}

#[cfg(feature = "native-webrtc")]
impl Default for NativeRtpSendState {
    fn default() -> Self {
        Self {
            next_sequence_number: 1,
            next_timestamp: 0,
        }
    }
}

#[cfg(feature = "native-webrtc")]
impl NativeRtpSendState {
    fn next_opus_packet(&mut self, payload: &[u8], marker: bool, timestamp_step: u32) -> RtpPacket {
        let sequence_number = self.next_sequence_number;
        let timestamp = self.next_timestamp;
        self.next_sequence_number = self.next_sequence_number.wrapping_add(1);
        self.next_timestamp = self.next_timestamp.wrapping_add(timestamp_step);

        RtpPacket {
            header: RtpHeader {
                version: 2,
                marker,
                payload_type: NATIVE_OPUS_RTP_PAYLOAD_TYPE,
                sequence_number,
                timestamp,
                ssrc: NATIVE_OPUS_RTP_SSRC,
                ..Default::default()
            },
            payload: payload.to_vec().into(),
        }
    }
}

#[cfg(feature = "native-webrtc")]
impl NativeWebRtcOfferFactory {
    fn from_env(
        client_action_tx: mpsc::Sender<ClientMessage>,
        native_event_tx: mpsc::Sender<TransportEvent>,
        vision: &VisionConfig,
    ) -> Self {
        let gather_timeout = Duration::from_millis(
            env::var(WEBRTC_NATIVE_GATHER_TIMEOUT_MS_ENV)
                .ok()
                .and_then(|value| value.trim().parse::<u64>().ok())
                .unwrap_or(DEFAULT_NATIVE_GATHER_TIMEOUT_MS)
                .max(1),
        );

        Self {
            sessions: Mutex::new(HashMap::new()),
            gather_timeout,
            client_action_tx,
            native_event_tx,
            vision: vision.clone(),
        }
    }

    async fn create_native_offer(&self, config: &RtcConfig) -> Result<RtcOffer> {
        let offer_started_at = Instant::now();
        let mut media_engine = MediaEngine::default();
        media_engine
            .register_default_codecs()
            .map_err(|err| anyhow!("WebRTC native codec 注册失败: {err}"))?;
        let mut setting_engine = SettingEngine::default();
        let network_types = native_ice_network_types_from_env()?;
        if !network_types.is_empty() {
            setting_engine.set_network_types(network_types.clone());
        }
        let api = APIBuilder::new()
            .with_media_engine(media_engine)
            .with_setting_engine(setting_engine)
            .build();
        let (ice_servers, skipped_ice_urls) = native_ice_servers_with_skipped_urls(config);
        if !skipped_ice_urls.is_empty() {
            warn!(
                session_id = %config.session_id,
                skipped_ice_url_count = skipped_ice_urls.len(),
                skipped_ice_urls = %skipped_ice_urls.join(","),
                "[RTC] native ICE profile skipped unsupported TURN TCP/TLS URLs"
            );
        }
        let rtc_config = RTCConfiguration {
            ice_servers,
            ..Default::default()
        };
        info!(
            session_id = %config.session_id,
            ice_networks = %native_ice_network_type_summary(&network_types),
            "[RTC] native ICE profile applied"
        );

        let peer_connection = api
            .new_peer_connection(rtc_config)
            .await
            .map_err(|err| anyhow!("WebRTC native peer connection 创建失败: {err}"))?;
        let peer_connection = Arc::new(peer_connection);
        let alive = Arc::new(AtomicBool::new(true));
        install_native_peer_connection_loggers(
            &config.session_id,
            Arc::clone(&peer_connection),
            self.native_event_tx.clone(),
            Arc::clone(&alive),
        );
        install_native_remote_track_receiver(
            &config.session_id,
            Arc::clone(&peer_connection),
            self.native_event_tx.clone(),
            native_remote_audio_events_enabled(config),
        );
        let local_candidates = Arc::new(Mutex::new(Vec::new()));
        install_native_ice_candidate_collector(
            &config.session_id,
            Arc::clone(&peer_connection),
            Arc::clone(&local_candidates),
        );
        let audio_track = native_opus_audio_track(config)?;
        peer_connection
            .add_track(Arc::clone(&audio_track) as Arc<dyn TrackLocal + Send + Sync>)
            .await
            .map_err(|err| anyhow!("WebRTC native audio track 添加失败: {err}"))?;
        info!(
            session_id = %config.session_id,
            track_id = NATIVE_AUDIO_TRACK_ID,
            stream_id = NATIVE_AUDIO_STREAM_ID,
            "[RTC] native Opus audio RTP track added"
        );
        let rtp_state = Arc::new(Mutex::new(NativeRtpSendState::default()));
        let data_channel = peer_connection
            .create_data_channel(NATIVE_DATA_CHANNEL_LABEL, None)
            .await
            .map_err(|err| anyhow!("WebRTC native data channel 创建失败: {err}"))?;
        let control_open = Arc::new(AtomicBool::new(false));
        install_native_data_channel_loggers(
            &config.session_id,
            Arc::clone(&data_channel),
            self.client_action_tx.clone(),
            self.native_event_tx.clone(),
            Arc::clone(&control_open),
            Arc::clone(&audio_track),
            Arc::clone(&rtp_state),
            native_rtp_probe_enabled(),
        );
        let vision_data_channel = if self.vision.enabled {
            let init = RTCDataChannelInit {
                ordered: Some(false),
                max_retransmits: Some(0),
                protocol: Some(VISION_DATA_CHANNEL_LABEL.to_string()),
                ..Default::default()
            };
            let channel = peer_connection
                .create_data_channel(VISION_DATA_CHANNEL_LABEL, Some(init))
                .await
                .map_err(|err| anyhow!("WebRTC native vision data channel 创建失败: {err}"))?;
            install_native_vision_data_channel(
                &config.session_id,
                Arc::clone(&channel),
                Arc::clone(&alive),
                self.vision.clone(),
            );
            Some(channel)
        } else {
            info!(
                session_id = %config.session_id,
                "[VISION] snapshot sender disabled"
            );
            None
        };

        let offer = peer_connection
            .create_offer(None)
            .await
            .map_err(|err| anyhow!("WebRTC native offer 创建失败: {err}"))?;
        let mut gather_complete = peer_connection.gathering_complete_promise().await;
        peer_connection
            .set_local_description(offer)
            .await
            .map_err(|err| anyhow!("WebRTC native local description 设置失败: {err}"))?;

        match time::timeout(self.gather_timeout, gather_complete.recv()).await {
            Ok(Some(())) => {
                debug!(
                    session_id = %config.session_id,
                    "[RTC] native ICE gathering completed"
                );
            }
            Ok(None) => {
                warn!(
                    session_id = %config.session_id,
                    "[RTC] native ICE gathering channel closed before completion"
                );
            }
            Err(_) => {
                warn!(
                    session_id = %config.session_id,
                    timeout_ms = self.gather_timeout.as_millis() as u64,
                    "[RTC] native ICE gathering timeout; sending current local description"
                );
            }
        }

        let local_description = peer_connection
            .local_description()
            .await
            .ok_or_else(|| anyhow!("WebRTC native local description 不可用"))?;
        ensure!(
            !local_description.sdp.trim().is_empty(),
            "WebRTC native offer 返回空 SDP"
        );
        let local_candidate_count = local_candidates
            .lock()
            .map_err(|_| anyhow!("WebRTC native local candidate store lock poisoned"))?
            .len();
        info!(
            session_id = %config.session_id,
            sdp_bytes = local_description.sdp.len(),
            local_candidates = local_candidate_count,
            gather_timeout_ms = self.gather_timeout.as_millis() as u64,
            duration_ms = offer_started_at.elapsed().as_millis() as u64,
            "[RTC] native offer ready"
        );
        self.remember_session(
            &config.session_id,
            NativePeerSession {
                peer_connection,
                data_channel,
                _vision_data_channel: vision_data_channel,
                control_open,
                alive,
                audio_track,
                rtp_state,
                local_candidates,
            },
        )?;

        Ok(RtcOffer {
            session_id: config.session_id.clone(),
            sdp: local_description.sdp,
        })
    }

    fn remember_session(&self, session_id: &str, session: NativePeerSession) -> Result<()> {
        let old_sessions = {
            let mut sessions = self
                .sessions
                .lock()
                .map_err(|_| anyhow!("WebRTC native session store lock poisoned"))?;
            let old_sessions = sessions
                .drain()
                .map(|(_, session)| session)
                .collect::<Vec<_>>();
            sessions.insert(session_id.to_string(), session);
            old_sessions
        };
        for old_session in old_sessions {
            close_native_peer_session(old_session, "replaced_by_new_session");
        }
        Ok(())
    }

    fn close_all_sessions(&self, reason: &'static str) {
        let Ok(mut sessions) = self.sessions.lock() else {
            return;
        };
        for (_, session) in sessions.drain() {
            close_native_peer_session(session, reason);
        }
    }

    fn peer_connection_for(&self, session_id: &str) -> Result<Arc<RTCPeerConnection>> {
        let sessions = self
            .sessions
            .lock()
            .map_err(|_| anyhow!("WebRTC native session store lock poisoned"))?;
        sessions
            .get(session_id)
            .map(|session| Arc::clone(&session.peer_connection))
            .ok_or_else(|| anyhow!("WebRTC native session 不存在: {session_id}"))
    }

    fn active_audio_sender(&self) -> Result<Option<NativeAudioSender>> {
        let sessions = self
            .sessions
            .lock()
            .map_err(|_| anyhow!("WebRTC native session store lock poisoned"))?;
        if sessions.is_empty() {
            return Ok(None);
        }
        if sessions.len() > 1 {
            warn!(
                session_count = sessions.len(),
                "[RTC] native RTP audio send skipped because multiple sessions are active"
            );
            return Ok(None);
        }
        let (session_id, session) = sessions
            .iter()
            .next()
            .ok_or_else(|| anyhow!("WebRTC native session store unexpectedly empty"))?;
        if !session.control_open.load(Ordering::SeqCst) {
            return Ok(None);
        }
        Ok(Some(NativeAudioSender {
            session_id: session_id.clone(),
            audio_track: Arc::clone(&session.audio_track),
            rtp_state: Arc::clone(&session.rtp_state),
        }))
    }

    fn active_control_sender(&self) -> Result<Option<NativeControlSender>> {
        let sessions = self
            .sessions
            .lock()
            .map_err(|_| anyhow!("WebRTC native session store lock poisoned"))?;
        if sessions.is_empty() {
            return Ok(None);
        }
        if sessions.len() > 1 {
            warn!(
                session_count = sessions.len(),
                "[RTC] native DataChannel control skipped because multiple sessions are active"
            );
            return Ok(None);
        }
        let (session_id, session) = sessions
            .iter()
            .next()
            .ok_or_else(|| anyhow!("WebRTC native session store unexpectedly empty"))?;
        if !session.control_open.load(Ordering::SeqCst) {
            return Ok(None);
        }
        Ok(Some(NativeControlSender {
            session_id: session_id.clone(),
            data_channel: Arc::clone(&session.data_channel),
        }))
    }

    fn has_active_audio_sender(&self) -> bool {
        let Ok(sessions) = self.sessions.lock() else {
            return false;
        };
        let Some((_session_id, session)) = sessions.iter().next() else {
            return false;
        };
        sessions.len() == 1 && session.control_open.load(Ordering::SeqCst)
    }

    fn drain_local_candidates_for(&self, session_id: &str) -> Result<Vec<RtcIceCandidate>> {
        let local_candidates = {
            let sessions = self
                .sessions
                .lock()
                .map_err(|_| anyhow!("WebRTC native session store lock poisoned"))?;
            sessions
                .get(session_id)
                .map(|session| Arc::clone(&session.local_candidates))
                .ok_or_else(|| anyhow!("WebRTC native session 不存在: {session_id}"))?
        };
        let mut guard = local_candidates
            .lock()
            .map_err(|_| anyhow!("WebRTC native local candidate store lock poisoned"))?;
        Ok(std::mem::take(&mut *guard))
    }
}

#[cfg(feature = "native-webrtc")]
impl Drop for NativeWebRtcOfferFactory {
    fn drop(&mut self) {
        self.close_all_sessions("offer_factory_drop");
    }
}

#[cfg(feature = "native-webrtc")]
impl RtcOfferFactory for NativeWebRtcOfferFactory {
    fn create_offer<'a>(
        &'a self,
        config: &'a RtcConfig,
    ) -> BoxFuture<'a, Result<RtcNegotiationAction>> {
        Box::pin(async move {
            let offer = self.create_native_offer(config).await?;
            Ok(RtcNegotiationAction::SendOffer(offer))
        })
    }

    fn audio_uplink_ready(&self) -> bool {
        self.has_active_audio_sender()
    }

    fn send_audio_frame<'a>(
        &'a self,
        header: &'a AudioFrameHeader,
        payload: &'a [u8],
    ) -> BoxFuture<'a, Result<bool>> {
        Box::pin(async move {
            if !is_native_opus_audio_frame_candidate(header) {
                return Ok(false);
            }
            let Some(sender) = self.active_audio_sender()? else {
                return Ok(false);
            };
            send_native_opus_audio_frame_as_rtp(
                &sender.session_id,
                &sender.audio_track,
                &sender.rtp_state,
                header,
                payload,
            )
            .await?;
            Ok(true)
        })
    }

    fn send_control<'a>(&'a self, message: &'a ClientMessage) -> BoxFuture<'a, Result<bool>> {
        Box::pin(async move {
            if !is_native_datachannel_control_message(message) {
                return Ok(false);
            }
            let Some(sender) = self.active_control_sender()? else {
                return Ok(false);
            };
            let payload =
                serde_json::to_string(message).context("序列化 WebRTC control 消息失败")?;
            sender
                .data_channel
                .send_text(payload)
                .await
                .map_err(|err| anyhow!("WebRTC native DataChannel control 发送失败: {err}"))?;
            debug!(
                session_id = %sender.session_id,
                message_type = native_client_message_type(message),
                "[RTC] native DataChannel control sent"
            );
            Ok(true)
        })
    }

    fn apply_answer<'a>(&'a self, answer: &'a RtcAnswer) -> BoxFuture<'a, Result<()>> {
        Box::pin(async move {
            let peer_connection = self.peer_connection_for(&answer.session_id)?;
            let description = RTCSessionDescription::answer(answer.sdp.clone())
                .map_err(|err| anyhow!("WebRTC native answer SDP 解析失败: {err}"))?;
            peer_connection
                .set_remote_description(description)
                .await
                .map_err(|err| anyhow!("WebRTC native remote answer 设置失败: {err}"))?;
            Ok(())
        })
    }

    fn add_remote_candidate<'a>(
        &'a self,
        candidate: &'a RtcIceCandidate,
    ) -> BoxFuture<'a, Result<()>> {
        Box::pin(async move {
            let peer_connection = self.peer_connection_for(&candidate.session_id)?;
            let candidate = RTCIceCandidateInit {
                candidate: candidate.candidate.clone(),
                sdp_mid: candidate.sdp_mid.clone(),
                sdp_mline_index: candidate.sdp_mline_index,
                username_fragment: None,
            };
            peer_connection
                .add_ice_candidate(candidate)
                .await
                .map_err(|err| anyhow!("WebRTC native remote ICE candidate 添加失败: {err}"))?;
            Ok(())
        })
    }

    fn drain_local_candidates(&self, session_id: &str) -> Result<Vec<RtcIceCandidate>> {
        self.drain_local_candidates_for(session_id)
    }

    fn close_sessions(&self, reason: &'static str) {
        self.close_all_sessions(reason);
    }
}

#[cfg(feature = "native-webrtc")]
fn native_ice_servers_with_skipped_urls(config: &RtcConfig) -> (Vec<RTCIceServer>, Vec<String>) {
    let mut skipped_urls = Vec::new();
    let servers = config
        .ice_servers
        .iter()
        .filter_map(|server| {
            let urls: Vec<String> = server
                .urls
                .iter()
                .filter_map(|url| {
                    if native_ice_url_supported(url) {
                        Some(url.clone())
                    } else {
                        skipped_urls.push(url.clone());
                        None
                    }
                })
                .collect();
            if urls.is_empty() {
                return None;
            }
            Some(RTCIceServer {
                urls,
                username: server.username.clone().unwrap_or_default(),
                credential: server.credential.clone().unwrap_or_default(),
            })
        })
        .collect();
    (servers, skipped_urls)
}

#[cfg(feature = "native-webrtc")]
fn native_ice_url_supported(url: &str) -> bool {
    let normalized = url.trim().to_ascii_lowercase();
    if normalized.starts_with("turn:") || normalized.starts_with("turns:") {
        return !normalized.contains("transport=tcp");
    }
    true
}

#[cfg(feature = "native-webrtc")]
fn native_ice_network_types_from_env() -> Result<Vec<NetworkType>> {
    let raw = env::var(WEBRTC_NATIVE_ICE_NETWORK_TYPES_ENV)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_NATIVE_ICE_NETWORK_TYPES.to_string());
    parse_native_ice_network_types(&raw)
}

#[cfg(feature = "native-webrtc")]
fn parse_native_ice_network_types(raw: &str) -> Result<Vec<NetworkType>> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(Vec::new());
    }
    if raw.eq_ignore_ascii_case("all") {
        return Ok(vec![NetworkType::Udp4, NetworkType::Udp6]);
    }

    let mut values = Vec::new();
    for part in raw.split(',') {
        let normalized = part.trim().to_ascii_lowercase();
        if normalized.is_empty() {
            continue;
        }
        let network_type = match normalized.as_str() {
            "udp4" | "ipv4" | "v4" => NetworkType::Udp4,
            "udp6" | "ipv6" | "v6" => NetworkType::Udp6,
            other => {
                return Err(anyhow!(
                    "WEBRTC_NATIVE_ICE_NETWORK_TYPES 不支持: {other}. 可选值: udp4、udp4,udp6、all"
                ));
            }
        };
        if !values.contains(&network_type) {
            values.push(network_type);
        }
    }
    Ok(values)
}

#[cfg(feature = "native-webrtc")]
fn native_ice_network_type_summary(network_types: &[NetworkType]) -> String {
    if network_types.is_empty() {
        "default".to_string()
    } else {
        network_types
            .iter()
            .map(|network_type| network_type.to_string())
            .collect::<Vec<_>>()
            .join(",")
    }
}

#[cfg(feature = "native-webrtc")]
fn native_ice_route(
    local_type: RTCIceCandidateType,
    remote_type: RTCIceCandidateType,
) -> &'static str {
    if matches!(local_type, RTCIceCandidateType::Relay)
        || matches!(remote_type, RTCIceCandidateType::Relay)
    {
        "turn"
    } else if matches!(local_type, RTCIceCandidateType::Unspecified)
        && matches!(remote_type, RTCIceCandidateType::Unspecified)
    {
        "unknown"
    } else {
        "stun_or_direct"
    }
}

#[cfg(feature = "native-webrtc")]
fn native_ice_route_detail(pair: &RTCIceCandidatePair) -> String {
    format!("{}-{}", pair.local.typ, pair.remote.typ)
}

#[cfg(feature = "native-webrtc")]
fn native_opus_audio_track(config: &RtcConfig) -> Result<Arc<TrackLocalStaticRTP>> {
    ensure!(
        config.media.audio.codec.eq_ignore_ascii_case("opus"),
        "WebRTC native audio track 仅支持 Opus，当前 codec={}",
        config.media.audio.codec
    );
    Ok(Arc::new(TrackLocalStaticRTP::new(
        RTCRtpCodecCapability {
            mime_type: MIME_TYPE_OPUS.to_owned(),
            ..Default::default()
        },
        NATIVE_AUDIO_TRACK_ID.to_string(),
        NATIVE_AUDIO_STREAM_ID.to_string(),
    )))
}

#[cfg(feature = "native-webrtc")]
fn native_remote_audio_events_enabled(config: &RtcConfig) -> bool {
    config.media.audio.downlink_transport.as_deref() == Some("webrtc_rtp")
}

#[cfg(feature = "native-webrtc")]
fn install_native_ice_candidate_collector(
    session_id: &str,
    peer_connection: Arc<RTCPeerConnection>,
    local_candidates: Arc<Mutex<Vec<RtcIceCandidate>>>,
) {
    let session_id = session_id.to_string();
    peer_connection.on_ice_candidate(Box::new(move |candidate| {
        let session_id = session_id.clone();
        let local_candidates = Arc::clone(&local_candidates);
        Box::pin(async move {
            let Some(candidate) = candidate else {
                return;
            };
            let candidate = match candidate.to_json() {
                Ok(candidate) => candidate,
                Err(err) => {
                    warn!(error = %err, session_id = %session_id, "[RTC] native local ICE candidate export failed");
                    return;
                }
            };
            if candidate.candidate.trim().is_empty() {
                return;
            }
            let rtc_candidate = RtcIceCandidate {
                session_id: session_id.clone(),
                candidate: candidate.candidate,
                sdp_mid: candidate.sdp_mid,
                sdp_mline_index: candidate.sdp_mline_index,
            };
            match local_candidates.lock() {
                Ok(mut guard) => guard.push(rtc_candidate),
                Err(_) => {
                    warn!(session_id = %session_id, "[RTC] native local ICE candidate store lock poisoned");
                }
            }
        })
    }));
}

#[cfg(feature = "native-webrtc")]
fn install_native_remote_track_receiver(
    session_id: &str,
    peer_connection: Arc<RTCPeerConnection>,
    native_event_tx: mpsc::Sender<TransportEvent>,
    emit_audio_frames: bool,
) {
    let session_id = session_id.to_string();
    peer_connection.on_track(Box::new(move |track: Arc<TrackRemote>, _, _| {
        let session_id = session_id.clone();
        let native_event_tx = native_event_tx.clone();
        Box::pin(async move {
            let track_id = track.id();
            let stream_id = track.stream_id();
            let codec = track.codec();
            info!(
                session_id = %session_id,
                track_id = %track_id,
                stream_id = %stream_id,
                codec = %codec.capability.mime_type,
                payload_type = track.payload_type(),
                emit_audio_frames,
                "[RTC] native remote RTP track opened"
            );

            let mut packet_count = 0u64;
            while let Ok((packet, _)) = track.read_rtp().await {
                packet_count += 1;
                if packet_count == 1 || packet_count % 100 == 0 {
                    debug!(
                        session_id = %session_id,
                        track_id = %track_id,
                        packet_count,
                        payload_type = packet.header.payload_type,
                        sequence_number = packet.header.sequence_number,
                        timestamp = packet.header.timestamp,
                        payload_bytes = packet.payload.len(),
                        emit_audio_frames,
                        "[RTC] native remote RTP packet received"
                    );
                }

                if !emit_audio_frames {
                    continue;
                }
                let frame = match native_downlink_audio_frame_from_rtp(&packet) {
                    Ok(frame) => frame,
                    Err(err) => {
                        warn!(
                            error = %err,
                            session_id = %session_id,
                            track_id = %track_id,
                            sequence_number = packet.header.sequence_number,
                            "[RTC] native remote RTP packet ignored"
                        );
                        continue;
                    }
                };
                if native_event_tx
                    .send(TransportEvent::AudioFrame(frame))
                    .await
                    .is_err()
                {
                    warn!(
                        session_id = %session_id,
                        track_id = %track_id,
                        "[RTC] native remote RTP audio event dropped because receiver closed"
                    );
                    break;
                }
            }
            info!(
                session_id = %session_id,
                track_id = %track_id,
                packet_count,
                "[RTC] native remote RTP track closed"
            );
        })
    }));
}

#[cfg(feature = "native-webrtc")]
fn install_native_peer_connection_loggers(
    session_id: &str,
    peer_connection: Arc<RTCPeerConnection>,
    native_event_tx: mpsc::Sender<TransportEvent>,
    alive: Arc<AtomicBool>,
) {
    let state_session_id = session_id.to_string();
    peer_connection.on_peer_connection_state_change(Box::new(move |state: RTCPeerConnectionState| {
        let session_id = state_session_id.clone();
        let native_event_tx = native_event_tx.clone();
        let alive = Arc::clone(&alive);
        Box::pin(async move {
            let stale = !alive.load(Ordering::SeqCst);
            info!(session_id = %session_id, state = %state, stale, "[RTC] native peer connection state changed");
            if stale {
                return;
            }
            if matches!(
                state,
                RTCPeerConnectionState::Failed | RTCPeerConnectionState::Closed
            ) && native_event_tx
                .send(TransportEvent::Error(anyhow!(
                    "WebRTC native peer connection {session_id} state changed to {state}"
                )))
                .await
                .is_err()
            {
                warn!(
                    session_id = %session_id,
                    state = %state,
                    "[RTC] native peer failure event dropped because receiver closed"
                );
            }
        })
    }));

    let gather_session_id = session_id.to_string();
    peer_connection.on_ice_gathering_state_change(Box::new(move |state: RTCIceGathererState| {
        let session_id = gather_session_id.clone();
        Box::pin(async move {
            debug!(session_id = %session_id, state = %state, "[RTC] native ICE gathering state changed");
        })
    }));

    let pair_session_id = session_id.to_string();
    peer_connection
        .sctp()
        .transport()
        .ice_transport()
        .on_selected_candidate_pair_change(Box::new(move |pair: RTCIceCandidatePair| {
            let session_id = pair_session_id.clone();
            Box::pin(async move {
                let route = native_ice_route(pair.local.typ, pair.remote.typ);
                let detail = native_ice_route_detail(&pair);
                info!(
                    session_id = %session_id,
                    ice_route = route,
                    ice_route_detail = %detail,
                    local_candidate_type = %pair.local.typ,
                    local_candidate_ip = %pair.local.address,
                    local_candidate_port = pair.local.port,
                    local_candidate_protocol = %pair.local.protocol,
                    local_candidate_related_ip = %pair.local.related_address,
                    local_candidate_related_port = pair.local.related_port,
                    remote_candidate_type = %pair.remote.typ,
                    remote_candidate_ip = %pair.remote.address,
                    remote_candidate_port = pair.remote.port,
                    remote_candidate_protocol = %pair.remote.protocol,
                    remote_candidate_related_ip = %pair.remote.related_address,
                    remote_candidate_related_port = pair.remote.related_port,
                    "[RTC] native selected ICE candidate pair"
                );
            })
        }));
}

#[cfg(feature = "native-webrtc")]
fn close_native_peer_session(session: NativePeerSession, reason: &'static str) {
    session.alive.store(false, Ordering::SeqCst);
    let peer_connection = Arc::clone(&session.peer_connection);
    let Ok(handle) = tokio::runtime::Handle::try_current() else {
        warn!(
            reason,
            "[RTC] native peer connection close skipped because Tokio runtime is unavailable"
        );
        return;
    };
    handle.spawn(async move {
        if let Err(err) = peer_connection.close().await {
            warn!(
                error = %err,
                reason,
                "[RTC] native peer connection close failed"
            );
        } else {
            debug!(reason, "[RTC] native peer connection closed");
        }
    });
}

#[cfg(feature = "native-webrtc")]
fn native_downlink_audio_frame_from_rtp(packet: &RtpPacket) -> Result<AudioFrame> {
    ensure!(
        packet.header.payload_type == NATIVE_OPUS_RTP_PAYLOAD_TYPE,
        "WebRTC downlink RTP payload type 不匹配: {} != {}",
        packet.header.payload_type,
        NATIVE_OPUS_RTP_PAYLOAD_TYPE
    );
    ensure!(
        !packet.payload.is_empty(),
        "WebRTC downlink RTP payload 为空"
    );
    ensure!(
        packet.payload.len() <= u16::MAX as usize,
        "WebRTC downlink RTP payload 过大: {} bytes",
        packet.payload.len()
    );

    let mut payload = Vec::with_capacity(OPUS_PACKET_STREAM_MAGIC.len() + 2 + packet.payload.len());
    payload.extend_from_slice(OPUS_PACKET_STREAM_MAGIC);
    payload.extend_from_slice(&(packet.payload.len() as u16).to_be_bytes());
    payload.extend_from_slice(&packet.payload);

    let sequence = u64::from(packet.header.sequence_number);
    Ok(AudioFrame {
        header: AudioFrameHeader {
            kind: "audio_frame".to_string(),
            version: AUDIO_FRAME_VERSION,
            encoding: "opus".to_string(),
            direction: Some("server_tts".to_string()),
            bot_id: None,
            trace_id: None,
            round_id: None,
            playback_id: None,
            utterance_id: None,
            seq: Some(sequence),
            chunk_seq: Some(sequence),
            timestamp_ms: None,
            duration_ms: Some(f64::from(OPUS_FRAME_DURATION_MS)),
            sample_rate: Some(OPUS_SAMPLE_RATE),
            channels: Some(OPUS_CHANNELS),
            opus_frame_ms: Some(OPUS_FRAME_DURATION_MS),
            packet_count: Some(1),
            stream_event: Some("chunk".to_string()),
        },
        payload,
    })
}

#[cfg(feature = "native-webrtc")]
#[allow(clippy::too_many_arguments)]
fn install_native_data_channel_loggers(
    session_id: &str,
    data_channel: Arc<RTCDataChannel>,
    client_action_tx: mpsc::Sender<ClientMessage>,
    native_event_tx: mpsc::Sender<TransportEvent>,
    control_open: Arc<AtomicBool>,
    audio_track: Arc<TrackLocalStaticRTP>,
    rtp_state: Arc<Mutex<NativeRtpSendState>>,
    rtp_probe_enabled: bool,
) {
    let open_session_id = session_id.to_string();
    data_channel.on_open(Box::new(move || {
        let session_id = open_session_id.clone();
        let client_action_tx = client_action_tx.clone();
        let control_open = Arc::clone(&control_open);
        let audio_track = Arc::clone(&audio_track);
        let rtp_state = Arc::clone(&rtp_state);
        Box::pin(async move {
            info!(session_id = %session_id, "[RTC] native data channel opened");
            control_open.store(true, Ordering::SeqCst);
            let ready = native_transport_ready_message(&session_id);
            if let Err(err) = client_action_tx.send(ready).await {
                warn!(error = %err, session_id = %session_id, "[RTC] native transport_ready enqueue failed");
            }
            if rtp_probe_enabled {
                if let Err(err) =
                    send_native_opus_rtp_probe(&session_id, &audio_track, &rtp_state).await
                {
                    warn!(error = %err, session_id = %session_id, "[RTC] native Opus RTP probe failed");
                }
            }
        })
    }));

    let message_session_id = session_id.to_string();
    data_channel.on_message(Box::new(move |message: DataChannelMessage| {
        let session_id = message_session_id.clone();
        let native_event_tx = native_event_tx.clone();
        Box::pin(async move {
            debug!(
                session_id = %session_id,
                bytes = message.data.len(),
                "[RTC] native data channel message received"
            );
            let server_message = match native_server_message_from_datachannel(&message.data) {
                Ok(server_message) => server_message,
                Err(err) => {
                    warn!(
                        error = %err,
                        session_id = %session_id,
                        "[RTC] native data channel message ignored"
                    );
                    return;
                }
            };
            debug!(
                session_id = %session_id,
                message_type = native_server_message_type(&server_message),
                "[RTC] native data channel server message accepted"
            );
            if native_event_tx
                .send(TransportEvent::Message(server_message))
                .await
                .is_err()
            {
                warn!(
                    session_id = %session_id,
                    "[RTC] native data channel event dropped because receiver closed"
                );
            }
        })
    }));
}

#[cfg(feature = "native-webrtc")]
fn install_native_vision_data_channel(
    session_id: &str,
    data_channel: Arc<RTCDataChannel>,
    alive: Arc<AtomicBool>,
    config: VisionConfig,
) {
    let open_session_id = session_id.to_string();
    let open_channel = Arc::clone(&data_channel);
    data_channel.on_open(Box::new(move || {
        let session_id = open_session_id.clone();
        let data_channel = Arc::clone(&open_channel);
        let alive = Arc::clone(&alive);
        let config = config.clone();
        Box::pin(async move {
            info!(
                session_id = %session_id,
                label = VISION_DATA_CHANNEL_LABEL,
                ordered = false,
                max_retransmits = 0,
                snapshot_path = %config.snapshot_path,
                scan_interval_ms = config.scan_interval_ms,
                "[VISION] native data channel opened"
            );
            tokio::spawn(run_native_vision_snapshot_loop(
                session_id,
                data_channel,
                alive,
                config,
            ));
        })
    }));

    let message_session_id = session_id.to_string();
    data_channel.on_message(Box::new(move |message: DataChannelMessage| {
        let session_id = message_session_id.clone();
        Box::pin(async move {
            if !message.is_string {
                warn!(
                    session_id = %session_id,
                    bytes = message.data.len(),
                    "[VISION] binary response ignored"
                );
                return;
            }
            match parse_vision_ack(&message.data) {
                Ok(ack) if ack.status == "accepted" => {
                    info!(
                        session_id = %session_id,
                        frame_id = ack.frame_id.as_deref().unwrap_or(""),
                        received_at_ms = ack.received_at_ms,
                        "[VISION] snapshot accepted by Gateway"
                    );
                }
                Ok(ack) => {
                    warn!(
                        session_id = %session_id,
                        frame_id = ack.frame_id.as_deref().unwrap_or(""),
                        reason = ack.reason.as_deref().unwrap_or("unknown"),
                        received_at_ms = ack.received_at_ms,
                        "[VISION] snapshot dropped by Gateway"
                    );
                }
                Err(err) => {
                    warn!(
                        session_id = %session_id,
                        error = %err,
                        bytes = message.data.len(),
                        "[VISION] invalid Gateway ACK ignored"
                    );
                }
            }
        })
    }));
}

#[cfg(feature = "native-webrtc")]
async fn run_native_vision_snapshot_loop(
    session_id: String,
    data_channel: Arc<RTCDataChannel>,
    alive: Arc<AtomicBool>,
    config: VisionConfig,
) {
    let scan_interval = Duration::from_millis(config.scan_interval_ms.max(100));
    let input_path = std::path::PathBuf::from(&config.snapshot_path);
    let policy = SnapshotPolicy::default();

    loop {
        if !alive.load(Ordering::SeqCst) || data_channel.ready_state() != RTCDataChannelState::Open
        {
            debug!(
                session_id = %session_id,
                "[VISION] snapshot sender stopped because channel is not open"
            );
            return;
        }
        if data_channel.buffered_amount().await > NATIVE_VISION_MAX_BUFFERED_BYTES {
            debug!(
                session_id = %session_id,
                "[VISION] snapshot scan skipped because DataChannel is backlogged"
            );
            time::sleep(scan_interval).await;
            continue;
        }

        let process_path = input_path.clone();
        let process_policy = policy.clone();
        let consumed = match tokio::task::spawn_blocking(move || {
            consume_snapshot(&process_path, &process_policy)
        })
        .await
        {
            Ok(Ok(consumed)) => consumed,
            Ok(Err(err)) => {
                warn!(
                    session_id = %session_id,
                    snapshot_path = %input_path.display(),
                    error = %err,
                    "[VISION] snapshot processing failed"
                );
                time::sleep(scan_interval).await;
                continue;
            }
            Err(err) => {
                warn!(
                    session_id = %session_id,
                    error = %err,
                    "[VISION] snapshot worker join failed"
                );
                time::sleep(scan_interval).await;
                continue;
            }
        };
        let Some(consumed) = consumed else {
            time::sleep(scan_interval).await;
            continue;
        };

        let now_ms = unix_time_millis();
        let capture_age_ms = now_ms.saturating_sub(consumed.captured_at_ms);
        if capture_age_ms > NATIVE_VISION_MAX_CAPTURE_AGE_MS {
            warn!(
                session_id = %session_id,
                capture_age_ms,
                "[VISION] stale local snapshot discarded before upload"
            );
            time::sleep(scan_interval).await;
            continue;
        }
        let frame_id = next_vision_frame_id();
        let jpeg_bytes = consumed.snapshot.jpeg.len();
        let width = consumed.snapshot.width;
        let height = consumed.snapshot.height;
        let passthrough = consumed.snapshot.passthrough;
        let chunks = match encode_vision_chunks(
            frame_id,
            consumed.captured_at_ms,
            &consumed.snapshot.jpeg,
        ) {
            Ok(chunks) => chunks,
            Err(err) => {
                warn!(
                    session_id = %session_id,
                    frame_id,
                    error = %err,
                    "[VISION] snapshot protocol encoding failed"
                );
                time::sleep(scan_interval).await;
                continue;
            }
        };
        let chunk_count = chunks.len();
        let send_started_at = Instant::now();
        for chunk in chunks {
            if let Err(err) = data_channel.send(&Bytes::from(chunk)).await {
                warn!(
                    session_id = %session_id,
                    frame_id,
                    error = %err,
                    "[VISION] snapshot chunk send failed"
                );
                return;
            }
        }
        time::sleep(scan_interval).await;
        info!(
            session_id = %session_id,
            frame_id,
            jpeg_bytes,
            width,
            height,
            chunk_count,
            capture_age_ms,
            passthrough,
            send_ms = send_started_at.elapsed().as_millis() as u64,
            "[VISION] snapshot chunks sent"
        );
    }
}

#[cfg(feature = "native-webrtc")]
fn next_vision_frame_id() -> u64 {
    let frame_id = NEXT_VISION_FRAME_ID.fetch_add(1, Ordering::Relaxed);
    if frame_id == 0 {
        NEXT_VISION_FRAME_ID.fetch_add(1, Ordering::Relaxed)
    } else {
        frame_id
    }
}

#[cfg(feature = "native-webrtc")]
fn unix_time_millis() -> i64 {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    i64::try_from(millis).unwrap_or(i64::MAX)
}

#[cfg(feature = "native-webrtc")]
async fn send_native_opus_rtp_probe(
    session_id: &str,
    audio_track: &Arc<TrackLocalStaticRTP>,
    rtp_state: &Arc<Mutex<NativeRtpSendState>>,
) -> Result<()> {
    let packets = {
        let mut guard = rtp_state
            .lock()
            .map_err(|_| anyhow!("WebRTC native RTP state lock poisoned"))?;
        (0..NATIVE_RTP_PROBE_PACKET_COUNT)
            .map(|index| {
                guard.next_opus_packet(
                    NATIVE_OPUS_RTP_PROBE_PAYLOAD,
                    index + 1 == NATIVE_RTP_PROBE_PACKET_COUNT,
                    NATIVE_OPUS_RTP_TIMESTAMP_STEP,
                )
            })
            .collect::<Vec<_>>()
    };
    for packet in &packets {
        audio_track
            .write_rtp(packet)
            .await
            .map_err(|err| anyhow!("WebRTC native Opus RTP probe 写入失败: {err}"))?;
        time::sleep(Duration::from_millis(20)).await;
    }
    info!(
        session_id = %session_id,
        packet_count = NATIVE_RTP_PROBE_PACKET_COUNT,
        payload_bytes = NATIVE_OPUS_RTP_PROBE_PAYLOAD.len(),
        "[RTC] native Opus RTP probe sent"
    );
    Ok(())
}

#[cfg(feature = "native-webrtc")]
async fn send_native_opus_audio_frame_as_rtp(
    session_id: &str,
    audio_track: &Arc<TrackLocalStaticRTP>,
    rtp_state: &Arc<Mutex<NativeRtpSendState>>,
    header: &AudioFrameHeader,
    payload: &[u8],
) -> Result<usize> {
    let packets = native_opus_rtp_packets_from_stream(header, payload, rtp_state)?;
    let packet_count = packets.len();
    let payload_bytes = packets
        .iter()
        .map(|packet| packet.payload.len())
        .sum::<usize>();

    for packet in &packets {
        audio_track
            .write_rtp(packet)
            .await
            .map_err(|err| anyhow!("WebRTC native Opus RTP audio 写入失败: {err}"))?;
    }

    debug!(
        session_id = %session_id,
        utterance_id = header.utterance_id.as_deref().unwrap_or("-"),
        chunk_seq = header.chunk_seq.unwrap_or_default(),
        packet_count,
        payload_bytes,
        "[RTC] native Opus RTP audio frame sent"
    );
    Ok(packet_count)
}

#[cfg(feature = "native-webrtc")]
fn native_opus_rtp_packets_from_stream(
    header: &AudioFrameHeader,
    payload: &[u8],
    rtp_state: &Arc<Mutex<NativeRtpSendState>>,
) -> Result<Vec<RtpPacket>> {
    let opus_packets = parse_opus_packet_stream(payload)?;
    if let Some(expected_packet_count) = header.packet_count {
        ensure!(
            expected_packet_count as usize == opus_packets.len(),
            "Opus packet_count 不匹配: header={}, payload={}",
            expected_packet_count,
            opus_packets.len()
        );
    }
    let timestamp_step = native_opus_rtp_timestamp_step(header)?;
    let mut guard = rtp_state
        .lock()
        .map_err(|_| anyhow!("WebRTC native RTP state lock poisoned"))?;
    let last_index = opus_packets.len().saturating_sub(1);
    Ok(opus_packets
        .into_iter()
        .enumerate()
        .map(|(index, packet)| guard.next_opus_packet(packet, index == last_index, timestamp_step))
        .collect())
}

#[cfg(feature = "native-webrtc")]
fn native_opus_rtp_timestamp_step(header: &AudioFrameHeader) -> Result<u32> {
    let frame_ms = header.opus_frame_ms.unwrap_or(20);
    ensure!(frame_ms > 0, "Opus RTP frame duration 不能为 0");
    Ok(48 * u32::from(frame_ms))
}

#[cfg(feature = "native-webrtc")]
fn is_native_opus_audio_frame_candidate(header: &AudioFrameHeader) -> bool {
    header.kind == "audio_frame"
        && header.encoding.eq_ignore_ascii_case("opus")
        && header.direction.as_deref() == Some("client_input")
        && header.stream_event.as_deref() == Some("chunk")
}

#[cfg(feature = "native-webrtc")]
fn is_native_datachannel_control_message(message: &ClientMessage) -> bool {
    matches!(
        message,
        ClientMessage::AudioStart { .. }
            | ClientMessage::TurnCandidate { .. }
            | ClientMessage::TurnCandidateCancel { .. }
            | ClientMessage::TurnCommitAck { .. }
            | ClientMessage::BargeInStart { .. }
            | ClientMessage::BargeInProbe { .. }
            | ClientMessage::BargeInCancel { .. }
            | ClientMessage::BargeInCommitAck { .. }
            | ClientMessage::AudioEnd { .. }
            | ClientMessage::AudioCancel { .. }
            | ClientMessage::Interrupt { .. }
            | ClientMessage::ClientEvent { .. }
            | ClientMessage::PlaybackComplete { .. }
            | ClientMessage::PlaybackInterrupted { .. }
            | ClientMessage::Heartbeat
    )
}

fn should_mirror_control_to_signaling_ws(message: &ClientMessage) -> bool {
    matches!(
        message,
        ClientMessage::Heartbeat | ClientMessage::ClientEvent { .. }
    )
}

fn native_client_message_type(message: &ClientMessage) -> &'static str {
    match message {
        ClientMessage::Register { .. } => "register",
        ClientMessage::Text { .. } => "text",
        ClientMessage::ClientEvent { .. } => "client_event",
        ClientMessage::AudioStart { .. } => "audio_start",
        ClientMessage::TurnCandidate { .. } => "turn_candidate",
        ClientMessage::TurnCandidateCancel { .. } => "turn_candidate_cancel",
        ClientMessage::TurnCommitAck { .. } => "turn_commit_ack",
        ClientMessage::BargeInStart { .. } => "barge_in_start",
        ClientMessage::BargeInProbe { .. } => "barge_in_probe",
        ClientMessage::BargeInCancel { .. } => "barge_in_cancel",
        ClientMessage::BargeInCommitAck { .. } => "barge_in_commit_ack",
        ClientMessage::AudioEnd { .. } => "audio_end",
        ClientMessage::AudioCancel { .. } => "audio_cancel",
        ClientMessage::Interrupt { .. } => "interrupt",
        ClientMessage::RtcOffer(_) => "rtc_offer",
        ClientMessage::RtcIceCandidate(_) => "rtc_ice_candidate",
        ClientMessage::TransportReady(_) => "transport_ready",
        ClientMessage::TransportFallbackStart(_) => "transport_fallback_start",
        ClientMessage::PlaybackComplete { .. } => "playback_complete",
        ClientMessage::PlaybackInterrupted { .. } => "playback_interrupted",
        ClientMessage::Heartbeat => "heartbeat",
    }
}

#[cfg(feature = "native-webrtc")]
fn native_server_message_from_datachannel(data: &[u8]) -> Result<ServerMessage> {
    let text = std::str::from_utf8(data).context("WebRTC control 下行消息不是 UTF-8")?;
    serde_json::from_str::<ServerMessage>(text)
        .with_context(|| format!("解析 WebRTC control 下行消息失败: {text}"))
}

#[cfg(feature = "native-webrtc")]
fn native_server_message_type(message: &ServerMessage) -> &'static str {
    match message {
        ServerMessage::Connected { .. } => "connected",
        ServerMessage::Registered { .. } => "registered",
        ServerMessage::Status { .. } => "status",
        ServerMessage::Text { .. } => "text",
        ServerMessage::ResponseAsr { .. } => "response.asr",
        ServerMessage::TurnCommitRequest { .. } => "turn_commit_request",
        ServerMessage::TurnCandidateDecision { .. } => "turn_candidate_decision",
        ServerMessage::BargeInDecision { .. } => "barge_in_decision",
        ServerMessage::Done { .. } => "done",
        ServerMessage::PlaybackStart { .. } => "playback_start",
        ServerMessage::PlaybackCancel { .. } => "playback_cancel",
        ServerMessage::Error { .. } => "error",
        ServerMessage::RtcConfig(_) => "rtc_config",
        ServerMessage::RtcAnswer(_) => "rtc_answer",
        ServerMessage::RtcIceCandidate(_) => "rtc_ice_candidate",
        ServerMessage::TransportFallbackAck(_) => "transport_fallback_ack",
        ServerMessage::HeartbeatAck => "heartbeat_ack",
        ServerMessage::Pong => "pong",
    }
}

#[cfg(feature = "native-webrtc")]
fn native_transport_ready_message(session_id: &str) -> ClientMessage {
    ClientMessage::TransportReady(TransportReady {
        session_id: session_id.to_string(),
        active_transport: TransportKind::WebRtcDataChannel.as_str().to_string(),
        audio: None,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(dead_code)]
pub enum WebRtcSignalingPhase {
    New,
    ConfigReceived,
    LocalOfferSent,
    RemoteAnswerReceived,
    RemoteCandidateReceived,
    TransportReady,
    FallbackActive,
    Failed,
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct WebRtcSignalingState {
    phase: WebRtcSignalingPhase,
    session_id: Option<String>,
    preflight_report: Option<RtcPreflightReport>,
    local_offer_sdp_bytes: Option<usize>,
    remote_answer_sdp_bytes: Option<usize>,
    remote_candidate_count: u32,
    active_transport: Option<String>,
    failure_reason: Option<String>,
}

#[allow(dead_code)]
impl WebRtcSignalingState {
    pub fn new() -> Self {
        Self {
            phase: WebRtcSignalingPhase::New,
            session_id: None,
            preflight_report: None,
            local_offer_sdp_bytes: None,
            remote_answer_sdp_bytes: None,
            remote_candidate_count: 0,
            active_transport: None,
            failure_reason: None,
        }
    }

    pub fn phase(&self) -> WebRtcSignalingPhase {
        self.phase
    }

    pub fn session_id(&self) -> Option<&str> {
        self.session_id.as_deref()
    }

    pub fn preflight_report(&self) -> Option<&RtcPreflightReport> {
        self.preflight_report.as_ref()
    }

    pub fn local_offer_sdp_bytes(&self) -> Option<usize> {
        self.local_offer_sdp_bytes
    }

    pub fn remote_answer_sdp_bytes(&self) -> Option<usize> {
        self.remote_answer_sdp_bytes
    }

    pub fn remote_candidate_count(&self) -> u32 {
        self.remote_candidate_count
    }

    pub fn active_transport(&self) -> Option<&str> {
        self.active_transport.as_deref()
    }

    pub fn failure_reason(&self) -> Option<&str> {
        self.failure_reason.as_deref()
    }

    pub fn apply_config(&mut self, config: &RtcConfig) -> Result<()> {
        self.ensure_phase(&[WebRtcSignalingPhase::New], "apply rtc config")?;

        self.session_id = Some(config.session_id.clone());
        self.preflight_report = Some(RtcPreflightReport::from_config(config));
        self.phase = WebRtcSignalingPhase::ConfigReceived;
        self.failure_reason = None;
        Ok(())
    }

    pub fn mark_local_offer_sent(&mut self, offer: &RtcOffer) -> Result<()> {
        self.ensure_session(&offer.session_id)?;
        self.ensure_phase(
            &[WebRtcSignalingPhase::ConfigReceived],
            "send local rtc offer",
        )?;

        self.local_offer_sdp_bytes = Some(offer.sdp.len());
        self.phase = WebRtcSignalingPhase::LocalOfferSent;
        Ok(())
    }

    pub fn apply_answer(&mut self, answer: &RtcAnswer) -> Result<()> {
        self.ensure_session(&answer.session_id)?;
        self.ensure_phase(
            &[WebRtcSignalingPhase::LocalOfferSent],
            "apply remote rtc answer",
        )?;

        self.remote_answer_sdp_bytes = Some(answer.sdp.len());
        self.phase = WebRtcSignalingPhase::RemoteAnswerReceived;
        Ok(())
    }

    pub fn add_remote_candidate(&mut self, candidate: &RtcIceCandidate) -> Result<()> {
        self.ensure_session(&candidate.session_id)?;
        self.ensure_phase(
            &[
                WebRtcSignalingPhase::RemoteAnswerReceived,
                WebRtcSignalingPhase::RemoteCandidateReceived,
                WebRtcSignalingPhase::TransportReady,
            ],
            "add remote ice candidate",
        )?;

        self.remote_candidate_count = self.remote_candidate_count.saturating_add(1);
        if self.phase != WebRtcSignalingPhase::TransportReady {
            self.phase = WebRtcSignalingPhase::RemoteCandidateReceived;
        }
        Ok(())
    }

    pub fn mark_transport_ready(&mut self, ready: &TransportReady) -> Result<()> {
        self.ensure_session(&ready.session_id)?;
        self.ensure_phase(
            &[
                WebRtcSignalingPhase::RemoteAnswerReceived,
                WebRtcSignalingPhase::RemoteCandidateReceived,
                WebRtcSignalingPhase::TransportReady,
            ],
            "mark rtc transport ready",
        )?;

        self.active_transport = Some(ready.active_transport.clone());
        self.phase = WebRtcSignalingPhase::TransportReady;
        Ok(())
    }

    pub fn apply_fallback_ack(&mut self, ack: &TransportFallbackAck) -> Result<()> {
        self.ensure_or_set_session(&ack.session_id)?;
        self.active_transport = Some(ack.active_transport.clone());
        if let Some(reason) = ack.reason.clone() {
            self.failure_reason = Some(reason);
        }
        self.phase = WebRtcSignalingPhase::FallbackActive;
        Ok(())
    }

    pub fn mark_failed(&mut self, reason: impl Into<String>) {
        self.failure_reason = Some(reason.into());
        self.phase = WebRtcSignalingPhase::Failed;
    }

    fn ensure_session(&self, session_id: &str) -> Result<()> {
        ensure!(
            self.session_id.as_deref() == Some(session_id),
            "WebRTC signaling session 不匹配: expected={:?}, actual={}",
            self.session_id,
            session_id
        );
        Ok(())
    }

    fn ensure_or_set_session(&mut self, session_id: &str) -> Result<()> {
        if self.session_id.is_none() {
            self.session_id = Some(session_id.to_string());
            return Ok(());
        }
        self.ensure_session(session_id)
    }

    fn ensure_phase(&self, allowed: &[WebRtcSignalingPhase], action: &str) -> Result<()> {
        ensure!(
            allowed.contains(&self.phase),
            "WebRTC signaling 状态不允许 {action}: current={:?}, allowed={:?}",
            self.phase,
            allowed
        );
        Ok(())
    }
}

fn is_blank_option(value: Option<&str>) -> bool {
    value.map(str::trim).unwrap_or_default().is_empty()
}

fn build_offer_factory_from_env(_vision: &VisionConfig) -> OfferFactoryBundle {
    if native_offer_factory_requested() {
        #[cfg(feature = "native-webrtc")]
        {
            let (client_action_tx, client_actions) = mpsc::channel(32);
            let (native_event_tx, native_events) = mpsc::channel(32);
            let factory =
                NativeWebRtcOfferFactory::from_env(client_action_tx, native_event_tx, _vision);
            info!(
                gather_timeout_ms = factory.gather_timeout.as_millis() as u64,
                "[RTC] 使用 Rust native WebRTC offer factory"
            );
            return OfferFactoryBundle {
                factory: Arc::new(factory),
                client_actions: Some(client_actions),
                native_events: Some(native_events),
            };
        }

        #[cfg(not(feature = "native-webrtc"))]
        {
            warn!(
                "[RTC] 已请求 Rust native WebRTC offer factory，但当前二进制未启用 native-webrtc feature"
            );
        }
    }

    if let Some(helper) = HelperProcessRtcOfferFactory::from_env() {
        info!(
            timeout_ms = helper.timeout.as_millis() as u64,
            "[RTC] 使用外部 WebRTC offer helper"
        );
        OfferFactoryBundle {
            factory: Arc::new(helper),
            client_actions: None,
            native_events: None,
        }
    } else {
        OfferFactoryBundle {
            factory: Arc::new(FallbackOnlyRtcOfferFactory),
            client_actions: None,
            native_events: None,
        }
    }
}

fn native_offer_factory_requested() -> bool {
    env::var(WEBRTC_OFFER_FACTORY_ENV)
        .map(|value| value.trim().eq_ignore_ascii_case("native"))
        .unwrap_or(true)
        || env::var(WEBRTC_NATIVE_ENABLED_ENV)
            .map(|value| {
                matches!(
                    value.trim().to_ascii_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            })
            .unwrap_or(false)
}

#[cfg(feature = "native-webrtc")]
fn native_rtp_probe_enabled() -> bool {
    env::var(WEBRTC_NATIVE_RTP_PROBE_ENABLED_ENV)
        .map(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

impl WebRtcVoiceTransport {
    pub async fn connect(
        options: WebRtcConnectOptions<'_>,
    ) -> Result<(Self, mpsc::Receiver<TransportEvent>)> {
        let connect_started_at = Instant::now();
        let (connection, gateway_events) = GatewayConnection::connect(
            options.signaling_url,
            options.connect_timeout,
            options.write_timeout,
        )
        .await?;
        let signaling_state = Arc::new(Mutex::new(WebRtcSignalingState::new()));
        let offer_factory_bundle = build_offer_factory_from_env(options.vision);
        let offer_factory = Arc::clone(&offer_factory_bundle.factory);
        let transport_events = spawn_event_adapter(
            gateway_events,
            Arc::clone(&signaling_state),
            connection.clone(),
            Arc::clone(&offer_factory),
            offer_factory_bundle.client_actions,
            offer_factory_bundle.native_events,
        );

        info!(
            signaling_url = options.signaling_url,
            connect_timeout_ms = options.connect_timeout.as_millis() as u64,
            audio_uplink_mode = options.audio_uplink_mode.as_str(),
            duration_ms = connect_started_at.elapsed().as_millis() as u64,
            "[RTC] WebRTC signaling bridge 已连接"
        );

        Ok((
            Self {
                connection,
                kind: TransportKind::HybridWebRtcWs,
                signaling_state,
                offer_factory,
                audio_uplink_mode: options.audio_uplink_mode,
            },
            transport_events,
        ))
    }

    #[allow(dead_code)]
    pub fn signaling_state_snapshot(&self) -> WebRtcSignalingState {
        signaling_state_snapshot(&self.signaling_state)
    }
}

fn spawn_event_adapter(
    mut gateway_events: mpsc::Receiver<GatewayEvent>,
    signaling_state: SharedSignalingState,
    connection: GatewayConnection,
    offer_factory: SharedOfferFactory,
    client_actions: Option<mpsc::Receiver<ClientMessage>>,
    native_events: Option<mpsc::Receiver<TransportEvent>>,
) -> mpsc::Receiver<TransportEvent> {
    let (transport_tx, transport_rx) = mpsc::channel(128);
    if let Some(mut client_actions) = client_actions {
        let action_connection = connection.clone();
        let action_state = Arc::clone(&signaling_state);
        tokio::spawn(async move {
            while let Some(client_message) = client_actions.recv().await {
                send_signaling_client_action(&action_state, &action_connection, client_message)
                    .await;
            }
        });
    }
    if let Some(mut native_events) = native_events {
        let native_transport_tx = transport_tx.clone();
        tokio::spawn(async move {
            while let Some(event) = native_events.recv().await {
                if native_transport_tx.send(event).await.is_err() {
                    break;
                }
            }
        });
    }
    tokio::spawn(async move {
        while let Some(event) = gateway_events.recv().await {
            let event = match event {
                GatewayEvent::Message(message) => {
                    let client_messages = observe_server_signaling_message(
                        &signaling_state,
                        &message,
                        offer_factory.as_ref(),
                    )
                    .await;
                    for client_message in client_messages {
                        send_signaling_client_action(&signaling_state, &connection, client_message)
                            .await;
                    }
                    TransportEvent::Message(message)
                }
                GatewayEvent::AudioFrame(frame) => TransportEvent::AudioFrame(frame),
                GatewayEvent::Closed => TransportEvent::Closed,
                GatewayEvent::Error(err) => TransportEvent::Error(err),
            };
            if transport_tx.send(event).await.is_err() {
                break;
            }
        }
    });
    transport_rx
}

async fn send_signaling_client_action(
    state: &SharedSignalingState,
    connection: &GatewayConnection,
    client_message: ClientMessage,
) {
    if let Err(err) = connection.send(&client_message).await {
        warn!(error = %err, "[RTC] signaling client action send failed");
        return;
    }
    if let Err(err) = observe_client_signaling_message(state, &client_message) {
        warn!(error = %err, "[RTC] signaling client action state update skipped");
    }
}

fn observe_client_signaling_message(
    state: &SharedSignalingState,
    client_message: &ClientMessage,
) -> Result<()> {
    if let ClientMessage::TransportReady(ready) = client_message {
        update_signaling_state(state, |signaling_state| {
            signaling_state.mark_transport_ready(ready)?;
            Ok(ServerSignalingUpdate {
                updated: true,
                offer_request: None,
            })
        })?;
        info!(
            session_id = %ready.session_id,
            active_transport = %ready.active_transport,
            audio_ready = ready.audio.is_some(),
            "[RTC] signaling state: transport ready"
        );
    }
    Ok(())
}

async fn observe_server_signaling_message(
    state: &SharedSignalingState,
    message: &ServerMessage,
    offer_factory: &dyn RtcOfferFactory,
) -> Vec<ClientMessage> {
    let is_signaling = matches!(
        message,
        ServerMessage::RtcConfig(_)
            | ServerMessage::RtcAnswer(_)
            | ServerMessage::RtcIceCandidate(_)
            | ServerMessage::TransportFallbackAck(_)
    );
    if !is_signaling {
        return Vec::new();
    }

    let update_result = update_signaling_state(state, |signaling_state| {
        apply_server_signaling_message_to_state(signaling_state, message)
    });

    match (message, update_result) {
        (ServerMessage::RtcConfig(config), Ok(update)) if update.updated => {
            let preflight = RtcPreflightReport::from_config(config);
            info!(
                session_id = %config.session_id,
                ice_servers = preflight.ice_server_count,
                stun_urls = preflight.stun_url_count,
                turn_urls = preflight.turn_url_count,
                turn_missing_credentials = preflight.turn_servers_missing_credentials,
                codec = %preflight.audio_codec,
                sample_rate = preflight.audio_sample_rate,
                ptime_ms = preflight.audio_ptime_ms,
                warnings = %preflight.warning_summary(),
                "[RTC] signaling state: config received"
            );
            if !preflight.warnings.is_empty() {
                warn!(
                    session_id = %config.session_id,
                    warnings = %preflight.warning_summary(),
                    "[RTC] preflight config has warnings"
                );
            }
            match update.offer_request {
                Some(config) => {
                    client_message_for_offer_request(state, &config, offer_factory).await
                }
                None => Vec::new(),
            }
        }
        (ServerMessage::RtcAnswer(answer), Ok(update)) if update.updated => {
            info!(
                session_id = %answer.session_id,
                sdp_bytes = answer.sdp.len(),
                "[RTC] signaling state: answer received"
            );
            if let Err(err) = offer_factory.apply_answer(answer).await {
                warn!(error = %err, "[RTC] offer factory failed to apply answer");
            }
            Vec::new()
        }
        (ServerMessage::RtcIceCandidate(candidate), Ok(update)) if update.updated => {
            debug!(
                session_id = %candidate.session_id,
                sdp_mid = candidate.sdp_mid.as_deref().unwrap_or("-"),
                sdp_mline_index = candidate.sdp_mline_index.unwrap_or_default(),
                "[RTC] signaling state: remote candidate received"
            );
            if let Err(err) = offer_factory.add_remote_candidate(candidate).await {
                warn!(error = %err, "[RTC] offer factory failed to add remote candidate");
            }
            Vec::new()
        }
        (ServerMessage::TransportFallbackAck(ack), Ok(update)) if update.updated => {
            info!(
                session_id = %ack.session_id,
                active_transport = %ack.active_transport,
                reason = ack.reason.as_deref().unwrap_or("-"),
                "[RTC] signaling state: fallback active"
            );
            Vec::new()
        }
        (_, Ok(_)) => Vec::new(),
        (_, Err(err)) => {
            warn!(error = %err, "[RTC] signaling state update skipped");
            Vec::new()
        }
    }
}

fn update_signaling_state(
    state: &SharedSignalingState,
    update: impl FnOnce(&mut WebRtcSignalingState) -> Result<ServerSignalingUpdate>,
) -> Result<ServerSignalingUpdate> {
    let mut guard = state
        .lock()
        .map_err(|_| anyhow!("WebRTC signaling state lock poisoned"))?;
    update(&mut guard)
}

fn signaling_state_snapshot(state: &SharedSignalingState) -> WebRtcSignalingState {
    match state.lock() {
        Ok(guard) => guard.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    }
}

fn apply_server_signaling_message_to_state(
    state: &mut WebRtcSignalingState,
    message: &ServerMessage,
) -> Result<ServerSignalingUpdate> {
    match message {
        ServerMessage::RtcConfig(config) => {
            state.apply_config(config)?;
            Ok(ServerSignalingUpdate {
                updated: true,
                offer_request: Some(config.clone()),
            })
        }
        ServerMessage::RtcAnswer(answer) => {
            state.apply_answer(answer)?;
            Ok(ServerSignalingUpdate {
                updated: true,
                offer_request: None,
            })
        }
        ServerMessage::RtcIceCandidate(candidate) => {
            state.add_remote_candidate(candidate)?;
            Ok(ServerSignalingUpdate {
                updated: true,
                offer_request: None,
            })
        }
        ServerMessage::TransportFallbackAck(ack) => {
            state.apply_fallback_ack(ack)?;
            Ok(ServerSignalingUpdate {
                updated: true,
                offer_request: None,
            })
        }
        _ => Ok(ServerSignalingUpdate {
            updated: false,
            offer_request: None,
        }),
    }
}

async fn client_message_for_offer_request(
    state: &SharedSignalingState,
    config: &RtcConfig,
    offer_factory: &dyn RtcOfferFactory,
) -> Vec<ClientMessage> {
    let offer_started_at = Instant::now();
    match offer_factory.create_offer(config).await {
        Ok(RtcNegotiationAction::SendOffer(offer)) => match mark_local_offer_sent(state, &offer) {
            Ok(()) => {
                let session_id = offer.session_id.clone();
                let mut messages = vec![ClientMessage::RtcOffer(offer)];
                let mut candidate_count = 0usize;
                match offer_factory.drain_local_candidates(&session_id) {
                    Ok(candidates) => {
                        candidate_count = candidates.len();
                        if !candidates.is_empty() {
                            debug!(
                                session_id = %session_id,
                                candidate_count = candidates.len(),
                                "[RTC] signaling state: local candidates queued"
                            );
                        }
                        messages.extend(candidates.into_iter().map(ClientMessage::RtcIceCandidate));
                    }
                    Err(err) => {
                        warn!(error = %err, session_id = %session_id, "[RTC] local ICE candidates unavailable");
                    }
                }
                info!(
                    session_id = %session_id,
                    local_candidates = candidate_count,
                    duration_ms = offer_started_at.elapsed().as_millis() as u64,
                    "[RTC] local offer created"
                );
                messages
            }
            Err(err) => {
                warn!(error = %err, "[RTC] local offer state update failed");
                vec![ClientMessage::TransportFallbackStart(
                    fallback_start_for_config(config),
                )]
            }
        },
        Ok(RtcNegotiationAction::StartFallback(fallback)) => {
            info!(
                session_id = %fallback.session_id,
                from_transport = %fallback.from_transport,
                to_transport = %fallback.to_transport,
                reason = %fallback.reason,
                duration_ms = offer_started_at.elapsed().as_millis() as u64,
                "[RTC] local offer unavailable; fallback requested"
            );
            vec![ClientMessage::TransportFallbackStart(fallback)]
        }
        Err(err) => {
            warn!(
                error = %err,
                duration_ms = offer_started_at.elapsed().as_millis() as u64,
                "[RTC] offer factory failed, fallback to websocket"
            );
            vec![ClientMessage::TransportFallbackStart(
                fallback_start_for_config(config),
            )]
        }
    }
}

fn mark_local_offer_sent(state: &SharedSignalingState, offer: &RtcOffer) -> Result<()> {
    let mut guard = state
        .lock()
        .map_err(|_| anyhow!("WebRTC signaling state lock poisoned"))?;
    guard.mark_local_offer_sent(offer)
}

fn fallback_start_for_config(config: &RtcConfig) -> TransportFallbackStart {
    TransportFallbackStart {
        session_id: config.session_id.clone(),
        from_transport: FALLBACK_FROM_TRANSPORT.to_string(),
        to_transport: FALLBACK_TO_TRANSPORT.to_string(),
        reason: FALLBACK_REASON_OFFER_UNAVAILABLE.to_string(),
        boundary: Some("rtc_config".to_string()),
    }
}

impl WebRtcVoiceTransport {
    #[allow(dead_code)]
    pub fn signaling_state(&self) -> WebRtcSignalingState {
        self.signaling_state_snapshot()
    }
}

impl Drop for WebRtcVoiceTransport {
    fn drop(&mut self) {
        self.offer_factory.close_sessions("transport_drop");
    }
}

impl VoiceTransport for WebRtcVoiceTransport {
    fn kind(&self) -> TransportKind {
        self.kind
    }

    fn audio_uplink_ready(&self) -> bool {
        if self.audio_uplink_mode != WebRtcAudioUplinkMode::RtpOnly {
            return true;
        }
        self.offer_factory.audio_uplink_ready()
    }

    fn send_control<'a>(&'a self, message: &'a ClientMessage) -> BoxFuture<'a, Result<()>> {
        Box::pin(async move {
            match self.offer_factory.send_control(message).await {
                Ok(true) => {
                    if should_mirror_control_to_signaling_ws(message) {
                        self.connection.send(message).await?;
                        debug!(
                            message_type = native_client_message_type(message),
                            "[RTC] control message mirrored to signaling WebSocket"
                        );
                    }
                    return Ok(());
                }
                Ok(false) => {}
                Err(err) => {
                    warn!(
                        error = %err,
                        "[RTC] native DataChannel control failed; continuing WebSocket control"
                    );
                }
            }
            self.connection.send(message).await
        })
    }

    fn send_audio_frame<'a>(
        &'a self,
        header: &'a AudioFrameHeader,
        payload: &'a [u8],
    ) -> BoxFuture<'a, Result<()>> {
        Box::pin(async move {
            match self.offer_factory.send_audio_frame(header, payload).await {
                Ok(true) => {
                    debug!(
                        utterance_id = header.utterance_id.as_deref().unwrap_or("-"),
                        chunk_seq = header.chunk_seq.unwrap_or_default(),
                        payload_bytes = payload.len(),
                        audio_uplink_mode = self.audio_uplink_mode.as_str(),
                        "[RTC] native RTP audio accepted"
                    );
                    if self.audio_uplink_mode == WebRtcAudioUplinkMode::RtpOnly {
                        return Ok(());
                    }
                }
                Ok(false) => {
                    if self.audio_uplink_mode == WebRtcAudioUplinkMode::RtpOnly {
                        return Err(anyhow!(
                            "RTC_AUDIO_UPLINK=rtp_only 但 native RTP audio track 尚未就绪"
                        ));
                    }
                }
                Err(err) => {
                    if self.audio_uplink_mode == WebRtcAudioUplinkMode::RtpOnly {
                        return Err(anyhow!(
                            "RTC_AUDIO_UPLINK=rtp_only native RTP audio 发送失败: {err}"
                        ));
                    }
                    warn!(
                        error = %err,
                        utterance_id = header.utterance_id.as_deref().unwrap_or("-"),
                        chunk_seq = header.chunk_seq.unwrap_or_default(),
                        "[RTC] native RTP audio send failed; continuing WebSocket audio frame"
                    );
                }
            }
            self.connection.send_audio_frame(header, payload).await
        })
    }
}

impl Default for WebRtcSignalingState {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::{
        apply_server_signaling_message_to_state, client_message_for_offer_request,
        observe_client_signaling_message, FallbackOnlyRtcOfferFactory,
        HelperProcessRtcOfferFactory, RtcNegotiationAction, RtcOfferFactory, RtcPreflightReport,
        WebRtcSignalingPhase, WebRtcSignalingState,
    };
    #[cfg(feature = "native-webrtc")]
    use crate::audio::opus_codec::{
        encode_pcm16_opus_packet_stream, parse_opus_packet_stream, OPUS_CHANNELS,
        OPUS_FRAME_DURATION_MS, OPUS_SAMPLE_RATE,
    };
    #[cfg(feature = "native-webrtc")]
    use crate::protocol::AudioFrameHeader;
    use crate::protocol::{
        rtc_signaling::{
            IceServer, RtcAnswer, RtcAudioConfig, RtcConfig, RtcIceCandidate, RtcMediaConfig,
            RtcOffer, TransportFallbackAck, TransportReady,
        },
        ClientMessage, ServerMessage,
    };
    use anyhow::Result;
    use futures_util::future::BoxFuture;
    use std::{
        sync::{Arc, Mutex},
        time::Duration,
    };
    #[cfg(feature = "native-webrtc")]
    use webrtc::{
        api::{media_engine::MediaEngine, APIBuilder},
        data_channel::RTCDataChannel,
        ice_transport::ice_candidate_type::RTCIceCandidateType,
        peer_connection::{
            configuration::RTCConfiguration, sdp::session_description::RTCSessionDescription,
        },
        track::track_local::{track_local_static_rtp::TrackLocalStaticRTP, TrackLocalWriter},
    };
    #[cfg(feature = "native-webrtc")]
    use webrtc_ice::network_type::NetworkType;

    #[test]
    fn signaling_state_accepts_expected_sequence() -> Result<()> {
        let mut state = WebRtcSignalingState::new();

        state.apply_config(&sample_config())?;
        assert_eq!(state.phase(), WebRtcSignalingPhase::ConfigReceived);
        assert_eq!(state.session_id(), Some("session_1"));
        assert!(state.preflight_report().is_some());

        state.mark_local_offer_sent(&sample_offer())?;
        assert_eq!(state.phase(), WebRtcSignalingPhase::LocalOfferSent);
        assert_eq!(state.local_offer_sdp_bytes(), Some(11));

        state.apply_answer(&sample_answer())?;
        assert_eq!(state.phase(), WebRtcSignalingPhase::RemoteAnswerReceived);
        assert_eq!(state.remote_answer_sdp_bytes(), Some(12));

        state.add_remote_candidate(&sample_candidate())?;
        assert_eq!(state.phase(), WebRtcSignalingPhase::RemoteCandidateReceived);
        assert_eq!(state.remote_candidate_count(), 1);

        state.mark_transport_ready(&sample_ready())?;
        assert_eq!(state.phase(), WebRtcSignalingPhase::TransportReady);
        assert_eq!(state.active_transport(), Some("webrtc_rtp"));
        Ok(())
    }

    #[test]
    fn rtc_preflight_report_classifies_ice_servers() {
        let mut config = sample_config();
        config.ice_servers = vec![
            IceServer {
                urls: vec!["stun:stun.example.com:3478".to_string()],
                username: None,
                credential: None,
            },
            IceServer {
                urls: vec![
                    "turn:turn.example.com:3478?transport=udp".to_string(),
                    "turns:turn.example.com:5349?transport=tcp".to_string(),
                ],
                username: Some("u".to_string()),
                credential: Some("p".to_string()),
            },
        ];

        let report = RtcPreflightReport::from_config(&config);

        assert_eq!(report.session_id, "session_1");
        assert_eq!(report.ice_server_count, 2);
        assert_eq!(report.stun_url_count, 1);
        assert_eq!(report.turn_url_count, 2);
        assert_eq!(report.turn_servers_missing_credentials, 0);
        assert!(report.has_stun());
        assert!(report.has_relay());
        assert!(report.warnings.is_empty(), "{:?}", report.warnings);
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_ice_servers_preserve_turn_credentials() {
        let mut config = sample_config();
        config.ice_servers = vec![
            IceServer {
                urls: vec!["stun:stun.example.com:3478".to_string()],
                username: None,
                credential: None,
            },
            IceServer {
                urls: vec!["turn:turn.example.com:3478?transport=udp".to_string()],
                username: Some("wzk".to_string()),
                credential: Some("secret".to_string()),
            },
        ];

        let servers = super::native_ice_servers_with_skipped_urls(&config).0;

        assert_eq!(servers.len(), 2);
        assert_eq!(servers[0].urls, vec!["stun:stun.example.com:3478"]);
        assert_eq!(servers[0].username, "");
        assert_eq!(servers[0].credential, "");
        assert_eq!(
            servers[1].urls,
            vec!["turn:turn.example.com:3478?transport=udp"]
        );
        assert_eq!(servers[1].username, "wzk");
        assert_eq!(servers[1].credential, "secret");
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_ice_servers_filter_unsupported_turn_tcp_urls() {
        let mut config = sample_config();
        config.ice_servers = vec![IceServer {
            urls: vec![
                "stun:frp.wzk.icu:3478".to_string(),
                "turn:frp.wzk.icu:3478?transport=udp".to_string(),
                "turn:frp.wzk.icu:3478?transport=tcp".to_string(),
                "turns:frp.wzk.icu:443?transport=tcp".to_string(),
            ],
            username: Some("wzkicu".to_string()),
            credential: Some("secret".to_string()),
        }];

        let (servers, skipped_urls) = super::native_ice_servers_with_skipped_urls(&config);

        assert_eq!(servers.len(), 1);
        assert_eq!(
            servers[0].urls,
            vec![
                "stun:frp.wzk.icu:3478".to_string(),
                "turn:frp.wzk.icu:3478?transport=udp".to_string(),
            ]
        );
        assert_eq!(servers[0].username, "wzkicu");
        assert_eq!(
            skipped_urls,
            vec![
                "turn:frp.wzk.icu:3478?transport=tcp".to_string(),
                "turns:frp.wzk.icu:443?transport=tcp".to_string(),
            ]
        );
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_ice_network_types_default_to_ipv4_udp() -> Result<()> {
        assert_eq!(
            super::parse_native_ice_network_types("udp4")?,
            vec![NetworkType::Udp4]
        );
        assert_eq!(
            super::parse_native_ice_network_types("udp4,udp6")?,
            vec![NetworkType::Udp4, NetworkType::Udp6]
        );
        assert!(super::parse_native_ice_network_types("tcp4").is_err());
        Ok(())
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_ice_route_classifies_relay_as_turn() {
        assert_eq!(
            super::native_ice_route(RTCIceCandidateType::Srflx, RTCIceCandidateType::Relay),
            "turn"
        );
        assert_eq!(
            super::native_ice_route(RTCIceCandidateType::Relay, RTCIceCandidateType::Host),
            "turn"
        );
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_ice_route_classifies_non_relay_as_stun_or_direct() {
        assert_eq!(
            super::native_ice_route(RTCIceCandidateType::Host, RTCIceCandidateType::Host),
            "stun_or_direct"
        );
        assert_eq!(
            super::native_ice_route(RTCIceCandidateType::Srflx, RTCIceCandidateType::Host),
            "stun_or_direct"
        );
        assert_eq!(
            super::native_ice_route(RTCIceCandidateType::Prflx, RTCIceCandidateType::Srflx),
            "stun_or_direct"
        );
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_ice_route_keeps_fully_unspecified_pair_unknown() {
        assert_eq!(
            super::native_ice_route(
                RTCIceCandidateType::Unspecified,
                RTCIceCandidateType::Unspecified,
            ),
            "unknown"
        );
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_opus_packet_stream_becomes_continuous_rtp_packets() -> Result<()> {
        let samples = vec![
            0i16;
            OPUS_SAMPLE_RATE as usize * OPUS_FRAME_DURATION_MS as usize / 1000
                * OPUS_CHANNELS as usize
                * 2
        ];
        let encoded = encode_pcm16_opus_packet_stream(&samples, OPUS_SAMPLE_RATE, OPUS_CHANNELS)?;
        let header = AudioFrameHeader::client_input_opus_chunk(
            "bot_1".to_string(),
            Some("trace_utterance_1".to_string()),
            "utterance_1".to_string(),
            1,
            OPUS_SAMPLE_RATE,
            OPUS_CHANNELS,
            f64::from(encoded.frame_duration_ms) * f64::from(encoded.packet_count),
            encoded.frame_duration_ms,
            encoded.packet_count,
        );
        let rtp_state = Arc::new(Mutex::new(super::NativeRtpSendState::default()));

        let first =
            super::native_opus_rtp_packets_from_stream(&header, &encoded.payload, &rtp_state)?;
        assert_eq!(encoded.packet_count as usize, first.len());
        assert_eq!(1, first[0].header.sequence_number);
        assert_eq!(0, first[0].header.timestamp);
        assert_eq!(
            super::NATIVE_OPUS_RTP_PAYLOAD_TYPE,
            first[0].header.payload_type
        );
        assert_eq!(super::NATIVE_OPUS_RTP_SSRC, first[0].header.ssrc);
        assert!(!first[0].header.marker);
        assert!(!first[0].payload.is_empty());
        assert_eq!(2, first[1].header.sequence_number);
        assert_eq!(
            super::NATIVE_OPUS_RTP_TIMESTAMP_STEP,
            first[1].header.timestamp
        );
        assert!(first[1].header.marker);

        let second =
            super::native_opus_rtp_packets_from_stream(&header, &encoded.payload, &rtp_state)?;
        assert_eq!(3, second[0].header.sequence_number);
        assert_eq!(
            super::NATIVE_OPUS_RTP_TIMESTAMP_STEP * 2,
            second[0].header.timestamp
        );
        assert_eq!(4, second[1].header.sequence_number);
        assert_eq!(
            super::NATIVE_OPUS_RTP_TIMESTAMP_STEP * 3,
            second[1].header.timestamp
        );
        Ok(())
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_downlink_rtp_packet_becomes_audio_frame() -> Result<()> {
        let packet = super::RtpPacket {
            header: super::RtpHeader {
                version: 2,
                payload_type: super::NATIVE_OPUS_RTP_PAYLOAD_TYPE,
                sequence_number: 42,
                timestamp: 960,
                ssrc: 0x575a_4b02,
                ..Default::default()
            },
            payload: vec![0xf8, 0xff, 0xfe].into(),
        };

        let frame = super::native_downlink_audio_frame_from_rtp(&packet)?;

        assert_eq!(frame.header.direction.as_deref(), Some("server_tts"));
        assert_eq!(frame.header.sample_rate, Some(OPUS_SAMPLE_RATE));
        assert_eq!(frame.header.channels, Some(OPUS_CHANNELS));
        assert_eq!(frame.header.opus_frame_ms, Some(OPUS_FRAME_DURATION_MS));
        assert_eq!(frame.header.packet_count, Some(1));
        assert_eq!(frame.header.chunk_seq, Some(42));
        let packets = parse_opus_packet_stream(&frame.payload)?;
        assert_eq!(packets, vec![&[0xf8, 0xff, 0xfe][..]]);
        Ok(())
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_datachannel_control_message_filter_keeps_signaling_on_ws() {
        assert!(super::is_native_datachannel_control_message(
            &ClientMessage::Interrupt { reason: None }
        ));
        assert!(super::is_native_datachannel_control_message(
            &ClientMessage::AudioStart {
                bot_id: "bot".to_string(),
                trace_id: Some("trace_u1".to_string()),
                utterance_id: "u1".to_string(),
                sample_rate: 16_000,
                channels: 1,
                opus_frame_ms: 20,
            }
        ));
        assert!(super::is_native_datachannel_control_message(
            &ClientMessage::PlaybackComplete {
                trace_id: Some("round_1".to_string()),
                round_id: "round_1".to_string(),
                playback_id: "playback_1".to_string(),
                first_audio_to_playback_start_ms: None,
                playback_start_to_complete_ms: None,
                pushed_chunks: 1,
                pushed_samples: 320,
                underrun_callbacks: 0,
                zero_filled_samples: 0,
                max_buffered_samples: 320,
            }
        ));
        assert!(super::is_native_datachannel_control_message(
            &ClientMessage::Heartbeat
        ));
        assert!(!super::is_native_datachannel_control_message(
            &ClientMessage::Register {
                robot_id: "robot".to_string(),
                robot_secret: None,
                client_type: "rust".to_string(),
            }
        ));
        assert!(!super::is_native_datachannel_control_message(
            &ClientMessage::RtcOffer(sample_offer())
        ));
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn heartbeat_is_mirrored_to_signaling_ws_for_idle_keepalive() {
        assert!(super::should_mirror_control_to_signaling_ws(
            &ClientMessage::Heartbeat
        ));
        assert!(super::should_mirror_control_to_signaling_ws(
            &ClientMessage::ClientEvent {
                event: "wake_idle".to_string(),
                bot_id: "xiaowen".to_string(),
                source: "hardware_wake".to_string(),
                trace_id: Some("wake_idle-1".to_string()),
                event_id: Some("wake_idle-1".to_string()),
            }
        ));
        assert!(!super::should_mirror_control_to_signaling_ws(
            &ClientMessage::Interrupt { reason: None }
        ));
        assert!(!super::should_mirror_control_to_signaling_ws(
            &ClientMessage::PlaybackInterrupted {
                trace_id: Some("round_1".to_string()),
                round_id: "round_1".to_string(),
                playback_id: "round_1:playback".to_string(),
                reason: Some("wake_interrupt".to_string()),
                first_audio_to_playback_start_ms: None,
                playback_start_to_complete_ms: None,
                pushed_chunks: 1,
                pushed_samples: 320,
                underrun_callbacks: 0,
                zero_filled_samples: 0,
                max_buffered_samples: 320,
            }
        ));
    }

    #[cfg(feature = "native-webrtc")]
    #[test]
    fn native_datachannel_server_message_parses_json() -> Result<()> {
        let message =
            super::native_server_message_from_datachannel(br#"{"type":"heartbeat_ack"}"#)?;
        assert!(matches!(message, ServerMessage::HeartbeatAck));

        let message = super::native_server_message_from_datachannel(
            br#"{"type":"error","code":"DATA_CHANNEL_CONTROL_ERROR","message":"bad"}"#,
        )?;
        assert!(matches!(
            message,
            ServerMessage::Error { code, message }
            if code == "DATA_CHANNEL_CONTROL_ERROR" && message == "bad"
        ));

        assert!(super::native_server_message_from_datachannel(b"pong:ping").is_err());
        Ok(())
    }

    #[cfg(feature = "native-webrtc")]
    #[tokio::test]
    async fn native_offer_factory_negotiates_loopback_datachannel() -> Result<()> {
        let snapshot_dir = std::env::temp_dir().join(format!(
            "rust-client-vision-loopback-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ));
        std::fs::create_dir_all(&snapshot_dir)?;
        let snapshot_path = snapshot_dir.join("pic.jpeg");
        let source_image = image::DynamicImage::ImageRgb8(image::RgbImage::from_pixel(
            640,
            360,
            image::Rgb([30, 90, 150]),
        ));
        let mut expected_vision_jpeg = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut expected_vision_jpeg, 75)
            .encode_image(&source_image)?;
        std::fs::write(&snapshot_path, &expected_vision_jpeg)?;

        let (client_action_tx, mut client_action_rx) = tokio::sync::mpsc::channel(4);
        let (native_event_tx, mut native_event_rx) = tokio::sync::mpsc::channel(4);
        let factory = super::NativeWebRtcOfferFactory {
            sessions: Mutex::new(std::collections::HashMap::new()),
            gather_timeout: Duration::from_secs(5),
            client_action_tx,
            native_event_tx,
            vision: super::VisionConfig {
                enabled: true,
                snapshot_path: snapshot_path.to_string_lossy().into_owned(),
                scan_interval_ms: 100,
            },
        };
        let config = sample_config();

        let action = factory.create_offer(&config).await?;
        let RtcNegotiationAction::SendOffer(offer) = action else {
            panic!("expected native offer action");
        };
        assert_eq!(offer.session_id, "session_1");
        assert!(offer.sdp.contains("m=audio"), "{:?}", offer.sdp);
        assert!(
            offer.sdp.to_ascii_lowercase().contains("opus"),
            "{:?}",
            offer.sdp
        );
        assert!(offer.sdp.contains("m=application"), "{:?}", offer.sdp);
        let offer_has_candidate = offer.sdp.contains("a=candidate:");

        let data_channel = {
            let sessions = factory.sessions.lock().unwrap();
            Arc::clone(
                &sessions
                    .get("session_1")
                    .expect("stored native session")
                    .data_channel,
            )
        };

        let mut media_engine = MediaEngine::default();
        media_engine.register_default_codecs()?;
        let api = APIBuilder::new().with_media_engine(media_engine).build();
        let answer_peer = Arc::new(api.new_peer_connection(RTCConfiguration::default()).await?);
        let (vision_chunk_tx, mut vision_chunk_rx) = tokio::sync::mpsc::channel(8);
        answer_peer.on_data_channel(Box::new(move |channel: Arc<RTCDataChannel>| {
            let vision_chunk_tx = vision_chunk_tx.clone();
            Box::pin(async move {
                if channel.label() == crate::snapshot::VISION_DATA_CHANNEL_LABEL {
                    channel.on_message(Box::new(move |message| {
                        let vision_chunk_tx = vision_chunk_tx.clone();
                        Box::pin(async move {
                            let _ = vision_chunk_tx.send(message.data.to_vec()).await;
                        })
                    }));
                    return;
                }
                let reply_channel = Arc::clone(&channel);
                channel.on_message(Box::new(move |message| {
                    let reply_channel = Arc::clone(&reply_channel);
                    Box::pin(async move {
                        let text = String::from_utf8_lossy(&message.data);
                        if text == "ping" {
                            let _ = reply_channel
                                .send_text(r#"{"type":"heartbeat_ack"}"#.to_string())
                                .await;
                        }
                    })
                }));
            })
        }));

        let remote_offer = RTCSessionDescription::offer(offer.sdp)?;
        answer_peer.set_remote_description(remote_offer).await?;
        let answer = answer_peer.create_answer(None).await?;
        let mut answer_gathering_complete = answer_peer.gathering_complete_promise().await;
        answer_peer.set_local_description(answer).await?;
        let _ = tokio::time::timeout(Duration::from_secs(5), answer_gathering_complete.recv())
            .await
            .map_err(|_| anyhow::anyhow!("timeout waiting for answer ICE gathering"))?;
        let answer = answer_peer
            .local_description()
            .await
            .ok_or_else(|| anyhow::anyhow!("answer local description missing"))?;
        let answer_has_candidate = answer.sdp.contains("a=candidate:");

        factory
            .apply_answer(&RtcAnswer {
                session_id: "session_1".to_string(),
                sdp: answer.sdp,
            })
            .await?;
        if !offer_has_candidate || !answer_has_candidate {
            eprintln!(
                "local WebRTC stack exposed no ICE candidates; offer_has_candidate={offer_has_candidate} answer_has_candidate={answer_has_candidate}; skipping DataChannel connectivity check"
            );
            answer_peer.close().await?;
            factory.peer_connection_for("session_1")?.close().await?;
            std::fs::remove_dir_all(&snapshot_dir)?;
            return Ok(());
        }
        let ready = tokio::time::timeout(Duration::from_secs(5), client_action_rx.recv())
            .await
            .map_err(|_| anyhow::anyhow!("timeout waiting for native transport_ready"))?
            .ok_or_else(|| anyhow::anyhow!("native transport_ready channel closed"))?;
        let ClientMessage::TransportReady(ready) = ready else {
            panic!("expected transport_ready action");
        };
        assert_eq!(ready.session_id, "session_1");
        assert_eq!(ready.active_transport, "webrtc_datachannel");
        assert!(ready.audio.is_none());

        let mut vision_parts: Option<Vec<Option<Vec<u8>>>> = None;
        let vision_deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        while vision_parts
            .as_ref()
            .map(|parts| parts.iter().any(Option::is_none))
            .unwrap_or(true)
        {
            let remaining = vision_deadline.saturating_duration_since(tokio::time::Instant::now());
            let chunk = tokio::time::timeout(remaining, vision_chunk_rx.recv())
                .await
                .map_err(|_| anyhow::anyhow!("timeout waiting for native vision chunk"))?
                .ok_or_else(|| anyhow::anyhow!("native vision chunk channel closed"))?;
            anyhow::ensure!(chunk.len() >= crate::snapshot::VISION_CHUNK_HEADER_BYTES);
            anyhow::ensure!(&chunk[0..4] == b"VIS1");
            let chunk_index = usize::from(u16::from_be_bytes(chunk[28..30].try_into()?));
            let chunk_count = usize::from(u16::from_be_bytes(chunk[30..32].try_into()?));
            let parts = vision_parts.get_or_insert_with(|| vec![None; chunk_count]);
            anyhow::ensure!(parts.len() == chunk_count);
            anyhow::ensure!(chunk_index < parts.len());
            parts[chunk_index] = Some(chunk[crate::snapshot::VISION_CHUNK_HEADER_BYTES..].to_vec());
        }
        let actual_vision_jpeg = vision_parts
            .unwrap()
            .into_iter()
            .flatten()
            .flatten()
            .collect::<Vec<_>>();
        assert_eq!(actual_vision_jpeg, expected_vision_jpeg);
        assert!(!snapshot_path.exists());

        data_channel.send_text("ping").await?;
        let event = tokio::time::timeout(Duration::from_secs(5), native_event_rx.recv())
            .await
            .map_err(|_| anyhow::anyhow!("timeout waiting for native data channel event"))?
            .ok_or_else(|| anyhow::anyhow!("native data channel event channel closed"))?;
        assert!(matches!(
            event,
            super::TransportEvent::Message(ServerMessage::HeartbeatAck)
        ));

        factory.peer_connection_for("session_1")?.close().await?;
        answer_peer.close().await?;
        std::fs::remove_dir_all(&snapshot_dir)?;
        Ok(())
    }

    #[cfg(feature = "native-webrtc")]
    #[tokio::test]
    async fn native_offer_factory_replaces_previous_session() -> Result<()> {
        let (client_action_tx, _client_action_rx) = tokio::sync::mpsc::channel(4);
        let (native_event_tx, _native_event_rx) = tokio::sync::mpsc::channel(4);
        let factory = super::NativeWebRtcOfferFactory {
            sessions: Mutex::new(std::collections::HashMap::new()),
            gather_timeout: Duration::from_millis(10),
            client_action_tx,
            native_event_tx,
            vision: super::VisionConfig {
                enabled: false,
                ..Default::default()
            },
        };
        let first = sample_config();
        let mut second = sample_config();
        second.session_id = "session_2".to_string();

        let _ = factory.create_offer(&first).await?;
        let _ = factory.create_offer(&second).await?;

        let sessions = factory.sessions.lock().unwrap();
        assert_eq!(1, sessions.len());
        assert!(!sessions.contains_key("session_1"));
        assert!(sessions.contains_key("session_2"));
        Ok(())
    }

    #[cfg(feature = "native-webrtc")]
    #[tokio::test]
    async fn native_offer_factory_receives_loopback_downlink_rtp() -> Result<()> {
        let (client_action_tx, mut client_action_rx) = tokio::sync::mpsc::channel(4);
        let (native_event_tx, mut native_event_rx) = tokio::sync::mpsc::channel(4);
        let factory = super::NativeWebRtcOfferFactory {
            sessions: Mutex::new(std::collections::HashMap::new()),
            gather_timeout: Duration::from_secs(5),
            client_action_tx,
            native_event_tx,
            vision: super::VisionConfig {
                enabled: false,
                ..Default::default()
            },
        };
        let mut config = sample_config();
        config.media.audio.downlink_transport = Some("webrtc_rtp".to_string());

        let action = factory.create_offer(&config).await?;
        let RtcNegotiationAction::SendOffer(offer) = action else {
            panic!("expected native offer action");
        };
        assert!(offer.sdp.contains("m=audio"), "{:?}", offer.sdp);
        assert!(offer.sdp.contains("a=sendrecv"), "{:?}", offer.sdp);
        let offer_has_candidate = offer.sdp.contains("a=candidate:");

        let mut media_engine = MediaEngine::default();
        media_engine.register_default_codecs()?;
        let api = APIBuilder::new().with_media_engine(media_engine).build();
        let answer_peer = Arc::new(api.new_peer_connection(RTCConfiguration::default()).await?);
        answer_peer.on_data_channel(Box::new(move |_channel: Arc<RTCDataChannel>| {
            Box::pin(async move {})
        }));
        let downlink_track = Arc::new(TrackLocalStaticRTP::new(
            super::RTCRtpCodecCapability {
                mime_type: super::MIME_TYPE_OPUS.to_owned(),
                ..Default::default()
            },
            "tts".to_string(),
            "voice".to_string(),
        ));
        answer_peer
            .add_track(Arc::clone(&downlink_track) as Arc<dyn super::TrackLocal + Send + Sync>)
            .await?;

        let remote_offer = RTCSessionDescription::offer(offer.sdp)?;
        answer_peer.set_remote_description(remote_offer).await?;
        let answer = answer_peer.create_answer(None).await?;
        let mut answer_gathering_complete = answer_peer.gathering_complete_promise().await;
        answer_peer.set_local_description(answer).await?;
        let _ = tokio::time::timeout(Duration::from_secs(5), answer_gathering_complete.recv())
            .await
            .map_err(|_| anyhow::anyhow!("timeout waiting for answer ICE gathering"))?;
        let answer = answer_peer
            .local_description()
            .await
            .ok_or_else(|| anyhow::anyhow!("answer local description missing"))?;
        assert!(answer.sdp.contains("m=audio"), "{:?}", answer.sdp);
        assert!(answer.sdp.contains("a=sendrecv"), "{:?}", answer.sdp);
        let answer_has_candidate = answer.sdp.contains("a=candidate:");

        factory
            .apply_answer(&RtcAnswer {
                session_id: "session_1".to_string(),
                sdp: answer.sdp,
            })
            .await?;
        if !offer_has_candidate || !answer_has_candidate {
            eprintln!(
                "local WebRTC stack exposed no ICE candidates; offer_has_candidate={offer_has_candidate} answer_has_candidate={answer_has_candidate}; skipping downlink RTP connectivity check"
            );
            answer_peer.close().await?;
            factory.peer_connection_for("session_1")?.close().await?;
            return Ok(());
        }

        let ready = tokio::time::timeout(Duration::from_secs(5), client_action_rx.recv())
            .await
            .map_err(|_| anyhow::anyhow!("timeout waiting for native transport_ready"))?
            .ok_or_else(|| anyhow::anyhow!("native transport_ready channel closed"))?;
        assert!(matches!(ready, ClientMessage::TransportReady(_)));

        let packet = super::RtpPacket {
            header: super::RtpHeader {
                version: 2,
                payload_type: super::NATIVE_OPUS_RTP_PAYLOAD_TYPE,
                sequence_number: 77,
                timestamp: super::NATIVE_OPUS_RTP_TIMESTAMP_STEP,
                ssrc: 0x575a_4b02,
                ..Default::default()
            },
            payload: vec![0xf8, 0xff, 0xfe].into(),
        };
        downlink_track.write_rtp(&packet).await?;

        let event = tokio::time::timeout(Duration::from_secs(5), native_event_rx.recv())
            .await
            .map_err(|_| anyhow::anyhow!("timeout waiting for native downlink RTP event"))?
            .ok_or_else(|| anyhow::anyhow!("native event channel closed"))?;
        let super::TransportEvent::AudioFrame(frame) = event else {
            panic!("expected native downlink audio frame event");
        };
        assert_eq!(frame.header.direction.as_deref(), Some("server_tts"));
        assert_eq!(frame.header.chunk_seq, Some(77));
        assert_eq!(
            parse_opus_packet_stream(&frame.payload)?,
            vec![&[0xf8, 0xff, 0xfe][..]]
        );

        factory.peer_connection_for("session_1")?.close().await?;
        answer_peer.close().await?;
        Ok(())
    }

    #[test]
    fn rtc_preflight_report_warns_on_unusable_ice_config() {
        let mut config = sample_config();
        config.ice_servers = vec![
            IceServer {
                urls: vec!["".to_string(), "https://example.com/ice".to_string()],
                username: None,
                credential: None,
            },
            IceServer {
                urls: vec!["turn:turn.example.com:3478".to_string()],
                username: Some(" ".to_string()),
                credential: None,
            },
        ];
        config.media.audio.codec = "pcm16".to_string();
        config.media.audio.sample_rate = 16_000;
        config.media.audio.ptime_ms = 30;

        let report = RtcPreflightReport::from_config(&config);

        assert_eq!(report.empty_url_count, 1);
        assert_eq!(report.unknown_url_count, 1);
        assert_eq!(report.turn_servers_missing_credentials, 1);
        assert!(report.warnings.contains(&"empty_ice_url".to_string()));
        assert!(report
            .warnings
            .contains(&"unknown_ice_url_scheme".to_string()));
        assert!(report
            .warnings
            .contains(&"turn_missing_username_or_credential".to_string()));
        assert!(report
            .warnings
            .contains(&"audio_codec_not_opus".to_string()));
        assert!(report
            .warnings
            .contains(&"audio_sample_rate_not_webrtc_opus_clock".to_string()));
        assert!(report.warnings.contains(&"audio_ptime_unusual".to_string()));
    }

    #[test]
    fn signaling_state_records_preflight_report() -> Result<()> {
        let mut state = WebRtcSignalingState::new();
        let mut config = sample_config();
        config.ice_servers = vec![IceServer {
            urls: vec!["stun:stun.example.com:3478".to_string()],
            username: None,
            credential: None,
        }];

        state.apply_config(&config)?;

        let report = state.preflight_report().expect("preflight report");
        assert_eq!(report.ice_server_count, 1);
        assert_eq!(report.stun_url_count, 1);
        assert_eq!(report.turn_url_count, 0);
        assert!(report.warnings.is_empty());
        Ok(())
    }

    #[test]
    fn signaling_state_rejects_answer_before_offer() -> Result<()> {
        let mut state = WebRtcSignalingState::new();

        state.apply_config(&sample_config())?;

        let err = state.apply_answer(&sample_answer()).unwrap_err();
        assert!(
            err.to_string()
                .contains("WebRTC signaling 状态不允许 apply remote rtc answer"),
            "unexpected error: {err}"
        );
        assert_eq!(state.phase(), WebRtcSignalingPhase::ConfigReceived);
        Ok(())
    }

    #[test]
    fn signaling_state_rejects_session_mismatch() -> Result<()> {
        let mut state = WebRtcSignalingState::new();
        let mut offer = sample_offer();

        state.apply_config(&sample_config())?;
        offer.session_id = "other_session".to_string();

        let err = state.mark_local_offer_sent(&offer).unwrap_err();
        assert!(
            err.to_string().contains("WebRTC signaling session 不匹配"),
            "unexpected error: {err}"
        );
        assert_eq!(state.phase(), WebRtcSignalingPhase::ConfigReceived);
        Ok(())
    }

    #[test]
    fn signaling_state_records_failure_reason() {
        let mut state = WebRtcSignalingState::new();

        state.mark_failed("dial_timeout");

        assert_eq!(state.phase(), WebRtcSignalingPhase::Failed);
        assert_eq!(state.failure_reason(), Some("dial_timeout"));
    }

    #[test]
    fn signaling_state_records_fallback_ack() -> Result<()> {
        let mut state = WebRtcSignalingState::new();

        state.apply_fallback_ack(&sample_fallback_ack())?;

        assert_eq!(state.phase(), WebRtcSignalingPhase::FallbackActive);
        assert_eq!(state.session_id(), Some("session_1"));
        assert_eq!(state.active_transport(), Some("websocket"));
        assert_eq!(state.failure_reason(), Some("media_not_ready"));
        Ok(())
    }

    #[test]
    fn client_transport_ready_updates_signaling_state() -> Result<()> {
        let state = Arc::new(Mutex::new(WebRtcSignalingState::new()));
        {
            let mut guard = state.lock().unwrap();
            guard.apply_config(&sample_config())?;
            guard.mark_local_offer_sent(&sample_offer())?;
            guard.apply_answer(&sample_answer())?;
        }

        observe_client_signaling_message(
            &state,
            &ClientMessage::TransportReady(sample_datachannel_ready()),
        )?;

        let snapshot = state.lock().unwrap().clone();
        assert_eq!(snapshot.phase(), WebRtcSignalingPhase::TransportReady);
        assert_eq!(snapshot.active_transport(), Some("webrtc_datachannel"));
        Ok(())
    }

    #[test]
    fn signaling_state_keeps_transport_ready_after_late_candidate() -> Result<()> {
        let mut state = WebRtcSignalingState::new();

        state.apply_config(&sample_config())?;
        state.mark_local_offer_sent(&sample_offer())?;
        state.apply_answer(&sample_answer())?;
        state.mark_transport_ready(&sample_datachannel_ready())?;
        state.add_remote_candidate(&sample_candidate())?;

        assert_eq!(state.phase(), WebRtcSignalingPhase::TransportReady);
        assert_eq!(state.remote_candidate_count(), 1);
        assert_eq!(state.active_transport(), Some("webrtc_datachannel"));
        Ok(())
    }

    #[test]
    fn server_signaling_message_updates_state() -> Result<()> {
        let mut state = WebRtcSignalingState::new();

        let update = apply_server_signaling_message_to_state(
            &mut state,
            &ServerMessage::RtcConfig(sample_config()),
        )?;

        assert!(update.updated);
        assert_eq!(state.phase(), WebRtcSignalingPhase::ConfigReceived);
        assert_eq!(state.session_id(), Some("session_1"));
        let Some(config) = update.offer_request else {
            panic!("expected offer request");
        };
        assert_eq!(config.session_id, "session_1");
        Ok(())
    }

    #[tokio::test]
    async fn fallback_only_offer_factory_plans_fallback() -> Result<()> {
        let factory = FallbackOnlyRtcOfferFactory;

        let action = factory.create_offer(&sample_config()).await?;

        let RtcNegotiationAction::StartFallback(fallback) = action else {
            panic!("expected fallback action");
        };
        assert_eq!(fallback.session_id, "session_1");
        assert_eq!(fallback.from_transport, "webrtc_rtp");
        assert_eq!(fallback.to_transport, "websocket");
        assert_eq!(fallback.reason, "client_webrtc_offer_not_available");
        assert_eq!(fallback.boundary.as_deref(), Some("rtc_config"));
        Ok(())
    }

    #[tokio::test]
    async fn helper_process_offer_factory_returns_offer() -> Result<()> {
        let factory = HelperProcessRtcOfferFactory {
            command: r#"printf '%s' '{"session_id":"session_1","sdp":"v=0 helper\r\n"}'"#
                .to_string(),
            timeout: Duration::from_secs(1),
        };

        let action = factory.create_offer(&sample_config()).await?;

        let RtcNegotiationAction::SendOffer(offer) = action else {
            panic!("expected offer action");
        };
        assert_eq!(offer.session_id, "session_1");
        assert_eq!(offer.sdp, "v=0 helper\r\n");
        Ok(())
    }

    #[tokio::test]
    async fn helper_process_offer_factory_rejects_session_mismatch() {
        let factory = HelperProcessRtcOfferFactory {
            command: r#"printf '%s' '{"session_id":"other_session","sdp":"v=0 helper\r\n"}'"#
                .to_string(),
            timeout: Duration::from_secs(1),
        };

        let err = factory.create_offer(&sample_config()).await.unwrap_err();

        assert!(
            err.to_string().contains("session_id 不匹配"),
            "unexpected error: {err}"
        );
    }

    #[tokio::test]
    async fn offer_factory_offer_marks_local_offer_sent() -> Result<()> {
        let state = Arc::new(Mutex::new(WebRtcSignalingState::new()));
        let config = sample_config();
        state.lock().unwrap().apply_config(&config)?;

        let messages = client_message_for_offer_request(&state, &config, &StaticOfferFactory).await;

        assert_eq!(messages.len(), 1);
        let ClientMessage::RtcOffer(offer) = &messages[0] else {
            panic!("expected rtc_offer action");
        };
        assert_eq!(offer.session_id, "session_1");
        assert_eq!(offer.sdp, "v=0 offer\r\n");
        let snapshot = state.lock().unwrap().clone();
        assert_eq!(snapshot.phase(), WebRtcSignalingPhase::LocalOfferSent);
        assert_eq!(snapshot.local_offer_sdp_bytes(), Some(11));
        Ok(())
    }

    #[tokio::test]
    async fn offer_factory_sends_local_candidates_after_offer() -> Result<()> {
        let state = Arc::new(Mutex::new(WebRtcSignalingState::new()));
        let config = sample_config();
        state.lock().unwrap().apply_config(&config)?;

        let messages =
            client_message_for_offer_request(&state, &config, &StaticCandidateOfferFactory).await;

        assert_eq!(messages.len(), 2);
        assert!(matches!(messages[0], ClientMessage::RtcOffer(_)));
        let ClientMessage::RtcIceCandidate(candidate) = &messages[1] else {
            panic!("expected rtc_ice_candidate after rtc_offer");
        };
        assert_eq!(candidate.session_id, "session_1");
        assert_eq!(candidate.candidate, sample_candidate().candidate);
        Ok(())
    }

    #[test]
    fn server_signaling_message_ignores_non_rtc_message() -> Result<()> {
        let mut state = WebRtcSignalingState::new();

        let update =
            apply_server_signaling_message_to_state(&mut state, &ServerMessage::HeartbeatAck)?;

        assert!(!update.updated);
        assert!(update.offer_request.is_none());
        assert_eq!(state.phase(), WebRtcSignalingPhase::New);
        Ok(())
    }

    struct StaticOfferFactory;

    impl RtcOfferFactory for StaticOfferFactory {
        fn create_offer<'a>(
            &'a self,
            _config: &'a RtcConfig,
        ) -> BoxFuture<'a, Result<RtcNegotiationAction>> {
            Box::pin(async move { Ok(RtcNegotiationAction::SendOffer(sample_offer())) })
        }
    }

    struct StaticCandidateOfferFactory;

    impl RtcOfferFactory for StaticCandidateOfferFactory {
        fn create_offer<'a>(
            &'a self,
            _config: &'a RtcConfig,
        ) -> BoxFuture<'a, Result<RtcNegotiationAction>> {
            Box::pin(async move { Ok(RtcNegotiationAction::SendOffer(sample_offer())) })
        }

        fn drain_local_candidates(&self, _session_id: &str) -> Result<Vec<RtcIceCandidate>> {
            Ok(vec![sample_candidate()])
        }
    }

    fn sample_config() -> RtcConfig {
        RtcConfig {
            session_id: "session_1".to_string(),
            ice_servers: Vec::new(),
            media: RtcMediaConfig {
                audio: RtcAudioConfig {
                    direction: "sendrecv".to_string(),
                    codec: "opus".to_string(),
                    sample_rate: 48_000,
                    channels: 1,
                    ptime_ms: 20,
                    downlink_transport: None,
                },
            },
        }
    }

    fn sample_offer() -> RtcOffer {
        RtcOffer {
            session_id: "session_1".to_string(),
            sdp: "v=0 offer\r\n".to_string(),
        }
    }

    fn sample_answer() -> RtcAnswer {
        RtcAnswer {
            session_id: "session_1".to_string(),
            sdp: "v=0 answer\r\n".to_string(),
        }
    }

    fn sample_candidate() -> RtcIceCandidate {
        RtcIceCandidate {
            session_id: "session_1".to_string(),
            candidate: "candidate:1 1 udp 2130706431 127.0.0.1 9 typ host".to_string(),
            sdp_mid: Some("audio".to_string()),
            sdp_mline_index: Some(0),
        }
    }

    fn sample_ready() -> TransportReady {
        TransportReady {
            session_id: "session_1".to_string(),
            active_transport: "webrtc_rtp".to_string(),
            audio: None,
        }
    }

    fn sample_datachannel_ready() -> TransportReady {
        TransportReady {
            session_id: "session_1".to_string(),
            active_transport: "webrtc_datachannel".to_string(),
            audio: None,
        }
    }

    fn sample_fallback_ack() -> TransportFallbackAck {
        TransportFallbackAck {
            session_id: "session_1".to_string(),
            active_transport: "websocket".to_string(),
            reason: Some("media_not_ready".to_string()),
        }
    }
}
