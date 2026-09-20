use super::{
    webrtc::{WebRtcConnectOptions, WebRtcVoiceTransport},
    ws::WsVoiceTransport,
    TransportEvent, TransportKind, TransportPolicy, VoiceTransport, WebRtcAudioUplinkMode,
};
use crate::config::VisionConfig;
use crate::protocol::{AudioFrameHeader, ClientMessage};
use anyhow::{bail, Result};
use futures_util::future::BoxFuture;
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::{info, warn};

pub struct HybridConnectOptions<'a> {
    pub url: &'a str,
    pub connect_timeout: Duration,
    pub write_timeout: Duration,
    pub policy: TransportPolicy,
    pub webrtc_enabled: bool,
    pub webrtc_connect_timeout: Duration,
    pub fallback_enabled: bool,
    pub webrtc_audio_uplink_mode: WebRtcAudioUplinkMode,
    pub vision: &'a VisionConfig,
}

#[derive(Clone)]
pub struct HybridTransport {
    active: ActiveTransport,
    policy: TransportPolicy,
}

#[derive(Clone)]
enum ActiveTransport {
    WebSocket(WsVoiceTransport),
    WebRtc(WebRtcVoiceTransport),
}

impl HybridTransport {
    pub async fn connect(
        options: HybridConnectOptions<'_>,
    ) -> Result<(Self, mpsc::Receiver<TransportEvent>)> {
        match options.policy {
            TransportPolicy::WebSocketOnly => {
                Self::connect_ws(options, "policy_websocket_only").await
            }
            TransportPolicy::AutoPreferWebRtc => {
                if options.webrtc_enabled {
                    match WebRtcVoiceTransport::connect(WebRtcConnectOptions {
                        signaling_url: options.url,
                        connect_timeout: options.webrtc_connect_timeout,
                        write_timeout: options.write_timeout,
                        audio_uplink_mode: options.webrtc_audio_uplink_mode,
                        vision: options.vision,
                    })
                    .await
                    {
                        Ok((transport, events)) => {
                            info!(
                                policy = options.policy.as_str(),
                                active_transport = transport.kind().as_str(),
                                rtc_audio_uplink = options.webrtc_audio_uplink_mode.as_str(),
                                "[TRANSPORT] 选择 WebRTC transport"
                            );
                            return Ok((
                                Self {
                                    active: ActiveTransport::WebRtc(transport),
                                    policy: options.policy,
                                },
                                events,
                            ));
                        }
                        Err(err) => {
                            warn!(
                                policy = options.policy.as_str(),
                                webrtc_connect_timeout_ms =
                                    options.webrtc_connect_timeout.as_millis() as u64,
                                fallback_enabled = options.fallback_enabled,
                                fallback_reason = "webrtc_connect_failed",
                                error = %err,
                                "[TRANSPORT] WebRTC 建连不可用，准备按策略降级到 WebSocket"
                            );
                        }
                    }
                } else {
                    info!(
                        policy = options.policy.as_str(),
                        fallback_enabled = options.fallback_enabled,
                        "[TRANSPORT] WebRTC 未启用，当前使用 WebSocket"
                    );
                }
                if !options.fallback_enabled {
                    bail!(
                        "TRANSPORT_POLICY=auto_prefer_webrtc 但 fallback 已关闭，且 WebRTC transport 不可用"
                    );
                }
                Self::connect_ws(options, "webrtc_connect_failed").await
            }
            TransportPolicy::WebRtcOnly => {
                if !options.webrtc_enabled {
                    bail!("TRANSPORT_POLICY=webrtc_only 需要 WEBRTC_ENABLED=true");
                }
                let (transport, events) = WebRtcVoiceTransport::connect(WebRtcConnectOptions {
                    signaling_url: options.url,
                    connect_timeout: options.webrtc_connect_timeout,
                    write_timeout: options.write_timeout,
                    audio_uplink_mode: options.webrtc_audio_uplink_mode,
                    vision: options.vision,
                })
                .await?;
                info!(
                    policy = options.policy.as_str(),
                    active_transport = transport.kind().as_str(),
                    rtc_audio_uplink = options.webrtc_audio_uplink_mode.as_str(),
                    "[TRANSPORT] 选择 WebRTC transport"
                );
                Ok((
                    Self {
                        active: ActiveTransport::WebRtc(transport),
                        policy: options.policy,
                    },
                    events,
                ))
            }
        }
    }

    async fn connect_ws(
        options: HybridConnectOptions<'_>,
        fallback_reason: &'static str,
    ) -> Result<(Self, mpsc::Receiver<TransportEvent>)> {
        info!(
            policy = options.policy.as_str(),
            active_transport = TransportKind::WebSocket.as_str(),
            fallback_reason,
            "[TRANSPORT] 选择 WebSocket transport"
        );
        let (transport, events) =
            WsVoiceTransport::connect(options.url, options.connect_timeout, options.write_timeout)
                .await?;
        Ok((
            Self {
                active: ActiveTransport::WebSocket(transport),
                policy: options.policy,
            },
            events,
        ))
    }

    pub fn policy(&self) -> TransportPolicy {
        self.policy
    }

    pub fn audio_uplink_ready(&self) -> bool {
        self.active().audio_uplink_ready()
    }

    fn active(&self) -> &dyn VoiceTransport {
        match &self.active {
            ActiveTransport::WebSocket(transport) => transport,
            ActiveTransport::WebRtc(transport) => transport,
        }
    }
}

impl VoiceTransport for HybridTransport {
    fn kind(&self) -> TransportKind {
        self.active().kind()
    }

    fn send_control<'a>(&'a self, message: &'a ClientMessage) -> BoxFuture<'a, Result<()>> {
        self.active().send_control(message)
    }

    fn send_audio_frame<'a>(
        &'a self,
        header: &'a AudioFrameHeader,
        payload: &'a [u8],
    ) -> BoxFuture<'a, Result<()>> {
        self.active().send_audio_frame(header, payload)
    }
}
