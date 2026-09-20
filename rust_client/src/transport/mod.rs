pub mod hybrid;
pub mod webrtc;
pub mod ws;

use crate::protocol::{AudioFrame, AudioFrameHeader, ClientMessage, ServerMessage};
use anyhow::{bail, Result};
use futures_util::future::BoxFuture;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransportPolicy {
    WebSocketOnly,
    WebRtcOnly,
    AutoPreferWebRtc,
}

impl TransportPolicy {
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "websocket_only" | "ws_only" | "websocket" | "ws" => Ok(Self::WebSocketOnly),
            "webrtc_only" | "rtc_only" | "webrtc" | "rtc" => Ok(Self::WebRtcOnly),
            "auto_prefer_webrtc" | "auto" | "hybrid" => Ok(Self::AutoPreferWebRtc),
            other => bail!(
                "TRANSPORT_POLICY 不支持: {other}. 可选值: websocket_only, webrtc_only, auto_prefer_webrtc"
            ),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::WebSocketOnly => "websocket_only",
            Self::WebRtcOnly => "webrtc_only",
            Self::AutoPreferWebRtc => "auto_prefer_webrtc",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WebRtcAudioUplinkMode {
    MirrorWebSocket,
    RtpOnly,
}

impl WebRtcAudioUplinkMode {
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "mirror_ws" | "mirror_websocket" | "mirror" | "dual" | "rtp_mirror" => {
                Ok(Self::MirrorWebSocket)
            }
            "rtp_only" | "webrtc_rtp" | "rtc_rtp" | "rtc_only" => Ok(Self::RtpOnly),
            other => bail!("RTC_AUDIO_UPLINK 不支持: {other}. 可选值: mirror_ws, rtp_only"),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::MirrorWebSocket => "mirror_ws",
            Self::RtpOnly => "rtp_only",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransportKind {
    WebSocket,
    WebRtcDataChannel,
    HybridWebRtcWs,
}

impl TransportKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::WebSocket => "websocket",
            Self::WebRtcDataChannel => "webrtc_datachannel",
            Self::HybridWebRtcWs => "hybrid_webrtc_ws",
        }
    }
}

#[derive(Debug)]
pub enum TransportEvent {
    Message(ServerMessage),
    AudioFrame(AudioFrame),
    Closed,
    Error(anyhow::Error),
}

pub trait VoiceTransport: Send + Sync {
    fn kind(&self) -> TransportKind;

    fn audio_uplink_ready(&self) -> bool {
        true
    }

    fn send_control<'a>(&'a self, message: &'a ClientMessage) -> BoxFuture<'a, Result<()>>;

    fn send_audio_frame<'a>(
        &'a self,
        header: &'a AudioFrameHeader,
        payload: &'a [u8],
    ) -> BoxFuture<'a, Result<()>>;
}

#[cfg(test)]
mod tests {
    use super::{TransportPolicy, WebRtcAudioUplinkMode};

    #[test]
    fn parses_transport_policy_aliases() {
        assert_eq!(
            TransportPolicy::parse("websocket_only").unwrap(),
            TransportPolicy::WebSocketOnly
        );
        assert_eq!(
            TransportPolicy::parse("ws").unwrap(),
            TransportPolicy::WebSocketOnly
        );
        assert_eq!(
            TransportPolicy::parse("webrtc_only").unwrap(),
            TransportPolicy::WebRtcOnly
        );
        assert_eq!(
            TransportPolicy::parse("auto").unwrap(),
            TransportPolicy::AutoPreferWebRtc
        );
    }

    #[test]
    fn rejects_unknown_transport_policy() {
        assert!(TransportPolicy::parse("udp_magic").is_err());
    }

    #[test]
    fn parses_webrtc_audio_uplink_aliases() {
        assert_eq!(
            WebRtcAudioUplinkMode::parse("mirror_ws").unwrap(),
            WebRtcAudioUplinkMode::MirrorWebSocket
        );
        assert_eq!(
            WebRtcAudioUplinkMode::parse("dual").unwrap(),
            WebRtcAudioUplinkMode::MirrorWebSocket
        );
        assert_eq!(
            WebRtcAudioUplinkMode::parse("rtp_only").unwrap(),
            WebRtcAudioUplinkMode::RtpOnly
        );
        assert_eq!(
            WebRtcAudioUplinkMode::parse("webrtc_rtp").unwrap(),
            WebRtcAudioUplinkMode::RtpOnly
        );
    }

    #[test]
    fn rejects_unknown_webrtc_audio_uplink_mode() {
        assert!(WebRtcAudioUplinkMode::parse("udp_magic").is_err());
    }
}
