use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};

#[path = "protocol/rtc_signaling.rs"]
pub mod rtc_signaling;

use self::rtc_signaling::{
    RtcAnswer, RtcConfig, RtcIceCandidate, RtcOffer, TransportFallbackAck, TransportFallbackStart,
    TransportReady,
};

pub const AUDIO_FRAME_MAGIC: &[u8; 4] = b"VAF1";
pub const AUDIO_FRAME_VERSION: u8 = 1;
pub const AUDIO_FRAME_HEADER_MAX_BYTES: usize = 4096;

#[derive(Debug, Clone, Serialize)]
#[allow(dead_code)]
#[serde(tag = "type")]
pub enum ClientMessage {
    #[serde(rename = "register")]
    Register {
        robot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        robot_secret: Option<String>,
        client_type: String,
    },

    #[serde(rename = "text")]
    Text {
        content: String,
        bot_id: String,
        source: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
    },

    #[serde(rename = "client_event")]
    ClientEvent {
        event: String,
        bot_id: String,
        source: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        event_id: Option<String>,
    },

    #[serde(rename = "audio_start")]
    AudioStart {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        sample_rate: u32,
        channels: u16,
        opus_frame_ms: u16,
    },

    #[serde(rename = "audio_end")]
    AudioEnd {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        duration_ms: f64,
        samples: u64,
        chunks: u64,
        packets: u64,
        pcm_bytes: u64,
        opus_bytes: u64,
    },

    #[serde(rename = "audio_cancel")]
    AudioCancel {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        reason: String,
    },

    #[serde(rename = "turn_candidate")]
    TurnCandidate {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
        silence_ms: u64,
        shadow: bool,
    },

    #[serde(rename = "turn_candidate_cancel")]
    TurnCandidateCancel {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
        reason: String,
    },

    #[serde(rename = "turn_commit_ack")]
    TurnCommitAck {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
        accepted: bool,
        reason: String,
    },

    #[serde(rename = "barge_in_start")]
    BargeInStart {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        round_id: String,
        playback_id: String,
        speech_epoch: u64,
        sample_rate: u32,
        channels: u16,
        opus_frame_ms: u16,
    },

    #[serde(rename = "barge_in_probe")]
    BargeInProbe {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        round_id: String,
        playback_id: String,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
    },

    #[serde(rename = "barge_in_cancel")]
    BargeInCancel {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        round_id: String,
        playback_id: String,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
        reason: String,
    },

    #[serde(rename = "barge_in_commit_ack")]
    BargeInCommitAck {
        bot_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        utterance_id: String,
        round_id: String,
        playback_id: String,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
        accepted: bool,
        reason: String,
    },

    #[serde(rename = "interrupt")]
    Interrupt {
        #[serde(skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
    },

    #[serde(rename = "rtc_offer")]
    RtcOffer(RtcOffer),

    #[serde(rename = "rtc_ice_candidate")]
    RtcIceCandidate(RtcIceCandidate),

    #[serde(rename = "transport_ready")]
    TransportReady(TransportReady),

    #[serde(rename = "transport_fallback_start")]
    TransportFallbackStart(TransportFallbackStart),

    #[serde(rename = "playback_complete")]
    PlaybackComplete {
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        round_id: String,
        playback_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        first_audio_to_playback_start_ms: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        playback_start_to_complete_ms: Option<f64>,
        pushed_chunks: u64,
        pushed_samples: u64,
        underrun_callbacks: u64,
        zero_filled_samples: u64,
        max_buffered_samples: u64,
    },

    #[serde(rename = "playback_interrupted")]
    PlaybackInterrupted {
        #[serde(skip_serializing_if = "Option::is_none")]
        trace_id: Option<String>,
        round_id: String,
        playback_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        first_audio_to_playback_start_ms: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        playback_start_to_complete_ms: Option<f64>,
        pushed_chunks: u64,
        pushed_samples: u64,
        underrun_callbacks: u64,
        zero_filled_samples: u64,
        max_buffered_samples: u64,
    },

    #[serde(rename = "heartbeat")]
    Heartbeat,
}

#[derive(Debug, Clone)]
pub struct AudioFrame {
    pub header: AudioFrameHeader,
    pub payload: Vec<u8>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioFrameHeader {
    #[serde(rename = "type")]
    pub kind: String,
    pub version: u8,
    pub encoding: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub direction: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bot_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub round_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub playback_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub utterance_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub seq: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub chunk_seq: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timestamp_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub duration_ms: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sample_rate: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub channels: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub opus_frame_ms: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub packet_count: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stream_event: Option<String>,
}

impl AudioFrameHeader {
    #[allow(clippy::too_many_arguments)]
    pub fn client_input_opus_chunk(
        bot_id: String,
        trace_id: Option<String>,
        utterance_id: String,
        chunk_seq: u64,
        sample_rate: u32,
        channels: u16,
        duration_ms: f64,
        opus_frame_ms: u16,
        packet_count: u32,
    ) -> Self {
        Self {
            kind: "audio_frame".to_string(),
            version: AUDIO_FRAME_VERSION,
            encoding: "opus".to_string(),
            direction: Some("client_input".to_string()),
            bot_id: Some(bot_id),
            trace_id,
            round_id: None,
            playback_id: None,
            utterance_id: Some(utterance_id),
            seq: None,
            chunk_seq: Some(chunk_seq),
            timestamp_ms: None,
            duration_ms: Some(duration_ms),
            sample_rate: Some(sample_rate),
            channels: Some(channels),
            opus_frame_ms: Some(opus_frame_ms),
            packet_count: Some(packet_count),
            stream_event: Some("chunk".to_string()),
        }
    }
}

pub fn encode_audio_frame(header: &AudioFrameHeader, payload: &[u8]) -> Result<Vec<u8>> {
    let header_json = serde_json::to_vec(header).context("序列化音频帧头失败")?;
    if header_json.len() > AUDIO_FRAME_HEADER_MAX_BYTES {
        bail!(
            "音频帧头过大: {} > {} bytes",
            header_json.len(),
            AUDIO_FRAME_HEADER_MAX_BYTES
        );
    }

    let header_len = u32::try_from(header_json.len()).context("音频帧头长度溢出")?;
    let mut frame =
        Vec::with_capacity(AUDIO_FRAME_MAGIC.len() + 4 + header_json.len() + payload.len());
    frame.extend_from_slice(AUDIO_FRAME_MAGIC);
    frame.extend_from_slice(&header_len.to_be_bytes());
    frame.extend_from_slice(&header_json);
    frame.extend_from_slice(payload);
    Ok(frame)
}

pub fn decode_audio_frame(frame: &[u8]) -> Result<AudioFrame> {
    if frame.len() < AUDIO_FRAME_MAGIC.len() + 4 {
        bail!("音频帧过短");
    }
    if &frame[..AUDIO_FRAME_MAGIC.len()] != AUDIO_FRAME_MAGIC {
        bail!("音频帧 magic 不匹配");
    }

    let header_start = AUDIO_FRAME_MAGIC.len() + 4;
    let header_len = u32::from_be_bytes([frame[4], frame[5], frame[6], frame[7]]) as usize;
    if header_len == 0 || header_len > AUDIO_FRAME_HEADER_MAX_BYTES {
        bail!("音频帧头长度非法: {header_len}");
    }
    let header_end = header_start
        .checked_add(header_len)
        .ok_or_else(|| anyhow!("音频帧头长度溢出"))?;
    if frame.len() < header_end {
        bail!("音频帧头不完整");
    }

    let header: AudioFrameHeader =
        serde_json::from_slice(&frame[header_start..header_end]).context("解析音频帧头失败")?;
    if header.kind != "audio_frame" {
        bail!("音频帧类型非法: {}", header.kind);
    }
    if header.version != AUDIO_FRAME_VERSION {
        bail!("不支持的音频帧版本: {}", header.version);
    }

    let payload = frame[header_end..].to_vec();
    if payload.is_empty() {
        bail!("音频帧 payload 为空");
    }

    Ok(AudioFrame { header, payload })
}

#[allow(dead_code)]
#[derive(Debug, Clone, Deserialize)]
pub struct AsrResultPayload {
    pub text: String,
    #[serde(default)]
    pub valid: bool,
    #[serde(default, rename = "final")]
    pub final_result: bool,
    #[serde(default)]
    pub asr_time_ms: Option<f64>,
}

#[allow(dead_code)]
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type")]
pub enum ServerMessage {
    #[serde(rename = "connected")]
    Connected { session_id: String },

    #[serde(rename = "registered")]
    Registered {
        session_id: String,
        robot_id: String,
        bot_id: String,
        #[serde(default)]
        bot_name: Option<String>,
        #[serde(default)]
        client_type: Option<String>,
        #[serde(default)]
        is_new_robot: bool,
    },

    #[serde(rename = "status")]
    Status {
        message: String,
        #[serde(default)]
        trace_id: Option<String>,
        #[serde(default)]
        round_id: Option<String>,
    },

    #[serde(rename = "text")]
    Text {
        content: String,
        #[serde(default)]
        asr_time_ms: Option<f64>,
        #[serde(default)]
        asr_metadata: Option<serde_json::Value>,
        #[serde(default)]
        trace_id: Option<String>,
        #[serde(default)]
        round_id: Option<String>,
    },

    #[serde(rename = "response.asr")]
    ResponseAsr {
        payload: AsrResultPayload,
        #[serde(default)]
        trace_id: Option<String>,
        #[serde(default)]
        utterance_id: Option<String>,
        #[serde(default)]
        round_id: Option<String>,
        #[serde(default)]
        playback_id: Option<String>,
    },

    #[serde(rename = "turn_commit_request")]
    TurnCommitRequest {
        #[serde(default)]
        trace_id: Option<String>,
        utterance_id: String,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
        #[serde(default)]
        asr_text: Option<String>,
        #[serde(default)]
        asr_time_ms: Option<f64>,
    },

    #[serde(rename = "turn_candidate_decision")]
    TurnCandidateDecision {
        #[serde(default)]
        trace_id: Option<String>,
        utterance_id: String,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
        decision: String,
        #[serde(default)]
        reason: Option<String>,
    },

    #[serde(rename = "barge_in_decision")]
    BargeInDecision {
        #[serde(default)]
        trace_id: Option<String>,
        utterance_id: String,
        round_id: String,
        playback_id: String,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
        decision: String,
        #[serde(default)]
        reason: Option<String>,
        #[serde(default)]
        asr_text: Option<String>,
        #[serde(default)]
        asr_time_ms: Option<f64>,
    },

    #[serde(rename = "done")]
    Done {
        #[serde(default)]
        exit: bool,
        #[serde(default)]
        trace_id: Option<String>,
        #[serde(default)]
        round_id: Option<String>,
        #[serde(default)]
        playback_id: Option<String>,
    },

    #[serde(rename = "playback_start")]
    PlaybackStart {
        #[serde(default)]
        trace_id: Option<String>,
        round_id: String,
        playback_id: String,
        #[serde(default)]
        rtp_start_sequence: Option<u16>,
    },

    #[serde(rename = "playback_cancel")]
    PlaybackCancel {
        #[serde(default)]
        trace_id: Option<String>,
        #[serde(default)]
        round_id: Option<String>,
        #[serde(default)]
        playback_id: Option<String>,
        #[serde(default)]
        reason: Option<String>,
    },

    #[serde(rename = "error")]
    Error { code: String, message: String },

    #[serde(rename = "rtc_config")]
    RtcConfig(RtcConfig),

    #[serde(rename = "rtc_answer")]
    RtcAnswer(RtcAnswer),

    #[serde(rename = "rtc_ice_candidate")]
    RtcIceCandidate(RtcIceCandidate),

    #[serde(rename = "transport_fallback_ack")]
    TransportFallbackAck(TransportFallbackAck),

    #[serde(rename = "heartbeat_ack")]
    HeartbeatAck,

    #[serde(rename = "pong")]
    Pong,
}

#[cfg(test)]
mod tests {
    use super::{
        decode_audio_frame, encode_audio_frame, AudioFrameHeader, ClientMessage, ServerMessage,
        AUDIO_FRAME_HEADER_MAX_BYTES,
    };
    use crate::protocol::rtc_signaling::{
        IceServer, RtcAudioConfig, RtcConfig, RtcIceCandidate, RtcMediaConfig, RtcOffer,
        TransportFallbackStart,
    };
    use serde_json::json;

    #[test]
    fn parses_playback_start_rtp_sequence_boundary() {
        let message = serde_json::from_str::<ServerMessage>(
            r#"{"type":"playback_start","trace_id":"t1","round_id":"r1","playback_id":"p1","rtp_start_sequence":65535}"#,
        )
        .unwrap();

        match message {
            ServerMessage::PlaybackStart {
                trace_id,
                round_id,
                playback_id,
                rtp_start_sequence,
            } => {
                assert_eq!(trace_id.as_deref(), Some("t1"));
                assert_eq!(round_id, "r1");
                assert_eq!(playback_id, "p1");
                assert_eq!(rtp_start_sequence, Some(u16::MAX));
            }
            other => panic!("unexpected message: {other:?}"),
        }
    }

    #[test]
    fn parses_playback_cancel() {
        let message = serde_json::from_str::<ServerMessage>(
            r#"{"type":"playback_cancel","round_id":"r1","playback_id":"p1","reason":"new_audio"}"#,
        )
        .unwrap();

        match message {
            ServerMessage::PlaybackCancel {
                round_id,
                playback_id,
                reason,
                trace_id: _,
            } => {
                assert_eq!(round_id.as_deref(), Some("r1"));
                assert_eq!(playback_id.as_deref(), Some("p1"));
                assert_eq!(reason.as_deref(), Some("new_audio"));
            }
            other => panic!("unexpected message: {other:?}"),
        }
    }

    #[test]
    fn parses_typed_asr_response() {
        let message = serde_json::from_str::<ServerMessage>(
            r#"{"version":1,"type":"response.asr","session_id":"rtc_1","trace_id":"trace_1","utterance_id":"utterance_1","round_id":"round_1","playback_id":"playback_1","timestamp_ms":1,"payload":{"text":"今天天气不错","valid":true,"final":true,"asr_time_ms":123.5}}"#,
        )
        .unwrap();

        match message {
            ServerMessage::ResponseAsr {
                payload,
                trace_id,
                utterance_id,
                round_id,
                playback_id,
            } => {
                assert_eq!(payload.text, "今天天气不错");
                assert!(payload.valid);
                assert!(payload.final_result);
                assert_eq!(payload.asr_time_ms, Some(123.5));
                assert_eq!(trace_id.as_deref(), Some("trace_1"));
                assert_eq!(utterance_id.as_deref(), Some("utterance_1"));
                assert_eq!(round_id.as_deref(), Some("round_1"));
                assert_eq!(playback_id.as_deref(), Some("playback_1"));
            }
            other => panic!("unexpected message: {other:?}"),
        }
    }

    #[test]
    fn serializes_playback_completion_reports() {
        let event = serde_json::to_value(ClientMessage::ClientEvent {
            event: "wake_idle".to_string(),
            bot_id: "xiaowen".to_string(),
            source: "hardware_wake".to_string(),
            trace_id: Some("wake_idle-1".to_string()),
            event_id: Some("wake_idle-1".to_string()),
        })
        .unwrap();

        assert_eq!(
            event,
            json!({
                "type": "client_event",
                "event": "wake_idle",
                "bot_id": "xiaowen",
                "source": "hardware_wake",
                "trace_id": "wake_idle-1",
                "event_id": "wake_idle-1"
            })
        );

        let complete = serde_json::to_value(ClientMessage::PlaybackComplete {
            trace_id: Some("r1".to_string()),
            round_id: "r1".to_string(),
            playback_id: "p1".to_string(),
            first_audio_to_playback_start_ms: Some(42.5),
            playback_start_to_complete_ms: Some(1200.0),
            pushed_chunks: 3,
            pushed_samples: 3200,
            underrun_callbacks: 1,
            zero_filled_samples: 160,
            max_buffered_samples: 6400,
        })
        .unwrap();

        assert_eq!(
            complete,
            json!({
                "type": "playback_complete",
                "trace_id": "r1",
                "round_id": "r1",
                "playback_id": "p1",
                "first_audio_to_playback_start_ms": 42.5,
                "playback_start_to_complete_ms": 1200.0,
                "pushed_chunks": 3,
                "pushed_samples": 3200,
                "underrun_callbacks": 1,
                "zero_filled_samples": 160,
                "max_buffered_samples": 6400
            })
        );

        let interrupted = serde_json::to_value(ClientMessage::PlaybackInterrupted {
            trace_id: Some("r1".to_string()),
            round_id: "r1".to_string(),
            playback_id: "p1".to_string(),
            reason: Some("wake_interrupt".to_string()),
            first_audio_to_playback_start_ms: Some(55.0),
            playback_start_to_complete_ms: None,
            pushed_chunks: 2,
            pushed_samples: 1600,
            underrun_callbacks: 0,
            zero_filled_samples: 0,
            max_buffered_samples: 3200,
        })
        .unwrap();

        assert_eq!(
            interrupted,
            json!({
                "type": "playback_interrupted",
                "trace_id": "r1",
                "round_id": "r1",
                "playback_id": "p1",
                "reason": "wake_interrupt",
                "first_audio_to_playback_start_ms": 55.0,
                "pushed_chunks": 2,
                "pushed_samples": 1600,
                "underrun_callbacks": 0,
                "zero_filled_samples": 0,
                "max_buffered_samples": 3200
            })
        );
    }

    #[test]
    fn serializes_streaming_audio_control_messages() {
        let start = serde_json::to_value(ClientMessage::AudioStart {
            bot_id: "xiaowen".to_string(),
            trace_id: Some("trace-u1".to_string()),
            utterance_id: "u1".to_string(),
            sample_rate: 16000,
            channels: 1,
            opus_frame_ms: 20,
        })
        .unwrap();

        assert_eq!(
            start,
            json!({
                "type": "audio_start",
                "bot_id": "xiaowen",
                "trace_id": "trace-u1",
                "utterance_id": "u1",
                "sample_rate": 16000,
                "channels": 1,
                "opus_frame_ms": 20
            })
        );

        let end = serde_json::to_value(ClientMessage::AudioEnd {
            bot_id: "xiaowen".to_string(),
            trace_id: Some("trace-u1".to_string()),
            utterance_id: "u1".to_string(),
            duration_ms: 1200.0,
            samples: 19200,
            chunks: 12,
            packets: 60,
            pcm_bytes: 38400,
            opus_bytes: 2400,
        })
        .unwrap();

        assert_eq!(end["type"], "audio_end");
        assert_eq!(end["bot_id"], "xiaowen");
        assert_eq!(end["trace_id"], "trace-u1");
        assert_eq!(end["utterance_id"], "u1");
        assert_eq!(end["chunks"], 12);
        assert_eq!(end["packets"], 60);

        let cancel = serde_json::to_value(ClientMessage::AudioCancel {
            bot_id: "xiaowen".to_string(),
            trace_id: Some("trace-u1".to_string()),
            utterance_id: "u1".to_string(),
            reason: "wake_interrupt".to_string(),
        })
        .unwrap();

        assert_eq!(
            cancel,
            json!({
                "type": "audio_cancel",
                "bot_id": "xiaowen",
                "trace_id": "trace-u1",
                "utterance_id": "u1",
                "reason": "wake_interrupt"
            })
        );

        let candidate = serde_json::to_value(ClientMessage::TurnCandidate {
            bot_id: "xiaowen".to_string(),
            trace_id: Some("trace-u1".to_string()),
            utterance_id: "u1".to_string(),
            candidate_seq: 1,
            speech_epoch: 0,
            audio_watermark: 19_200,
            silence_ms: 300,
            shadow: true,
        })
        .unwrap();

        assert_eq!(
            candidate,
            json!({
                "type": "turn_candidate",
                "bot_id": "xiaowen",
                "trace_id": "trace-u1",
                "utterance_id": "u1",
                "candidate_seq": 1,
                "speech_epoch": 0,
                "audio_watermark": 19200,
                "silence_ms": 300,
                "shadow": true
            })
        );

        let candidate_cancel = serde_json::to_value(ClientMessage::TurnCandidateCancel {
            bot_id: "xiaowen".to_string(),
            trace_id: Some("trace-u1".to_string()),
            utterance_id: "u1".to_string(),
            candidate_seq: 1,
            speech_epoch: 1,
            audio_watermark: 20_800,
            reason: "speech_resumed".to_string(),
        })
        .unwrap();
        assert_eq!(candidate_cancel["type"], "turn_candidate_cancel");
        assert_eq!(candidate_cancel["speech_epoch"], 1);
        assert_eq!(candidate_cancel["reason"], "speech_resumed");
    }

    #[test]
    fn round_trips_natural_barge_in_control_identity() {
        let probe = serde_json::to_value(ClientMessage::BargeInProbe {
            bot_id: "xiaowen".to_string(),
            trace_id: Some("trace-b1".to_string()),
            utterance_id: "barge-1".to_string(),
            round_id: "round-1".to_string(),
            playback_id: "playback-1".to_string(),
            candidate_seq: 2,
            speech_epoch: 1,
            audio_watermark: 19_200,
        })
        .unwrap();
        assert_eq!(probe["type"], "barge_in_probe");
        assert_eq!(probe["round_id"], "round-1");
        assert_eq!(probe["playback_id"], "playback-1");
        assert_eq!(probe["candidate_seq"], 2);
        assert_eq!(probe["audio_watermark"], 19_200);

        let interrupt = serde_json::to_value(ClientMessage::Interrupt {
            reason: Some("natural_barge_in".to_string()),
        })
        .unwrap();
        assert_eq!(
            interrupt,
            json!({"type": "interrupt", "reason": "natural_barge_in"})
        );

        let message = serde_json::from_str::<ServerMessage>(
            r#"{"type":"barge_in_decision","trace_id":"trace-b1","utterance_id":"barge-1","round_id":"round-1","playback_id":"playback-1","candidate_seq":2,"speech_epoch":1,"audio_watermark":19200,"decision":"new_intent","reason":"meaningful_speech","asr_text":"帮我查天气","asr_time_ms":81.5}"#,
        )
        .unwrap();
        match message {
            ServerMessage::BargeInDecision {
                utterance_id,
                round_id,
                playback_id,
                candidate_seq,
                speech_epoch,
                audio_watermark,
                decision,
                asr_text,
                ..
            } => {
                assert_eq!(utterance_id, "barge-1");
                assert_eq!(round_id, "round-1");
                assert_eq!(playback_id, "playback-1");
                assert_eq!(candidate_seq, 2);
                assert_eq!(speech_epoch, 1);
                assert_eq!(audio_watermark, 19_200);
                assert_eq!(decision, "new_intent");
                assert_eq!(asr_text.as_deref(), Some("帮我查天气"));
            }
            other => panic!("unexpected message: {other:?}"),
        }
    }

    #[test]
    fn turn_commit_request_deserializes_promoted_candidate() {
        let message = serde_json::from_value::<ServerMessage>(json!({
            "type": "turn_commit_request",
            "trace_id": "trace-1",
            "utterance_id": "utt-1",
            "candidate_seq": 2,
            "speech_epoch": 1,
            "audio_watermark": 24000,
            "asr_text": "好的，就这样吧",
            "asr_time_ms": 123.4
        }))
        .expect("deserialize turn commit");
        match message {
            ServerMessage::TurnCommitRequest {
                utterance_id,
                candidate_seq,
                speech_epoch,
                audio_watermark,
                asr_text,
                ..
            } => {
                assert_eq!(utterance_id, "utt-1");
                assert_eq!(candidate_seq, 2);
                assert_eq!(speech_epoch, 1);
                assert_eq!(audio_watermark, 24_000);
                assert_eq!(asr_text.as_deref(), Some("好的，就这样吧"));
            }
            other => panic!("unexpected message: {other:?}"),
        }
    }

    #[test]
    fn turn_candidate_decision_deserializes_continue_feedback() {
        let message = serde_json::from_value::<ServerMessage>(json!({
            "type": "turn_candidate_decision",
            "trace_id": "trace-1",
            "utterance_id": "utt-1",
            "candidate_seq": 2,
            "speech_epoch": 1,
            "audio_watermark": 24000,
            "decision": "continue",
            "reason": "models_continue"
        }))
        .expect("deserialize candidate decision");
        match message {
            ServerMessage::TurnCandidateDecision {
                utterance_id,
                candidate_seq,
                speech_epoch,
                audio_watermark,
                decision,
                reason,
                ..
            } => {
                assert_eq!(utterance_id, "utt-1");
                assert_eq!(candidate_seq, 2);
                assert_eq!(speech_epoch, 1);
                assert_eq!(audio_watermark, 24_000);
                assert_eq!(decision, "continue");
                assert_eq!(reason.as_deref(), Some("models_continue"));
            }
            other => panic!("unexpected message: {other:?}"),
        }
    }

    #[test]
    fn serializes_rtc_signaling_control_messages() {
        let offer = serde_json::to_value(ClientMessage::RtcOffer(RtcOffer {
            session_id: "sess_001".to_string(),
            sdp: "v=0\r\n...".to_string(),
        }))
        .unwrap();

        assert_eq!(
            offer,
            json!({
                "type": "rtc_offer",
                "session_id": "sess_001",
                "sdp": "v=0\r\n..."
            })
        );

        let candidate = serde_json::to_value(ClientMessage::RtcIceCandidate(RtcIceCandidate {
            session_id: "sess_001".to_string(),
            candidate: "candidate:1 1 udp 1 192.0.2.1 5000 typ host".to_string(),
            sdp_mid: Some("audio".to_string()),
            sdp_mline_index: Some(0),
        }))
        .unwrap();

        assert_eq!(candidate["type"], "rtc_ice_candidate");
        assert_eq!(candidate["session_id"], "sess_001");
        assert_eq!(candidate["sdp_mid"], "audio");
        assert_eq!(candidate["sdp_mline_index"], 0);

        let fallback = serde_json::to_value(ClientMessage::TransportFallbackStart(
            TransportFallbackStart {
                session_id: "sess_001".to_string(),
                from_transport: "webrtc_rtp".to_string(),
                to_transport: "websocket".to_string(),
                reason: "ice_timeout".to_string(),
                boundary: Some("next_turn".to_string()),
            },
        ))
        .unwrap();

        assert_eq!(fallback["type"], "transport_fallback_start");
        assert_eq!(fallback["reason"], "ice_timeout");
        assert_eq!(fallback["boundary"], "next_turn");
    }

    #[test]
    fn parses_rtc_config_server_message() {
        let message = serde_json::from_value::<ServerMessage>(json!({
            "type": "rtc_config",
            "session_id": "sess_001",
            "ice_servers": [
                {"urls": ["stun:stun.example.com:3478"]},
                {
                    "urls": ["turn:turn.example.com:3478?transport=udp"],
                    "username": "short_lived_user",
                    "credential": "short_lived_credential"
                }
            ],
            "media": {
                "audio": {
                    "direction": "sendrecv",
                    "codec": "opus",
                    "sample_rate": 48000,
                    "channels": 1,
                    "ptime_ms": 20,
                    "downlink_transport": "websocket"
                }
            }
        }))
        .unwrap();

        match message {
            ServerMessage::RtcConfig(config) => {
                assert_eq!(config.session_id, "sess_001");
                assert_eq!(config.ice_servers.len(), 2);
                assert_eq!(config.media.audio.codec, "opus");
                assert_eq!(config.media.audio.sample_rate, 48000);
                assert_eq!(
                    config.media.audio.downlink_transport.as_deref(),
                    Some("websocket")
                );
            }
            other => panic!("unexpected message: {other:?}"),
        }
    }

    #[test]
    fn rtc_config_struct_omits_empty_optional_credentials() {
        let config = RtcConfig {
            session_id: "sess_001".to_string(),
            ice_servers: vec![IceServer {
                urls: vec!["stun:stun.example.com:3478".to_string()],
                username: None,
                credential: None,
            }],
            media: RtcMediaConfig {
                audio: RtcAudioConfig {
                    direction: "sendrecv".to_string(),
                    codec: "opus".to_string(),
                    sample_rate: 48000,
                    channels: 1,
                    ptime_ms: 20,
                    downlink_transport: None,
                },
            },
        };

        let value = serde_json::to_value(config).unwrap();
        assert!(value["ice_servers"][0].get("username").is_none());
        assert!(value["ice_servers"][0].get("credential").is_none());
    }

    #[test]
    fn round_trips_streaming_binary_audio_chunk() {
        let header = AudioFrameHeader::client_input_opus_chunk(
            "xiaowen".to_string(),
            Some("trace-u1".to_string()),
            "u1".to_string(),
            7,
            16000,
            1,
            100.0,
            20,
            5,
        );
        let frame = encode_audio_frame(&header, b"OPUSRAW1\x00\x03abc").unwrap();
        let decoded = decode_audio_frame(&frame).unwrap();

        assert_eq!("audio_frame", decoded.header.kind);
        assert_eq!("opus", decoded.header.encoding);
        assert_eq!(Some("chunk"), decoded.header.stream_event.as_deref());
        assert_eq!(Some("trace-u1"), decoded.header.trace_id.as_deref());
        assert_eq!(Some("u1"), decoded.header.utterance_id.as_deref());
        assert_eq!(Some(7), decoded.header.chunk_seq);
        assert_eq!(Some(100.0), decoded.header.duration_ms);
        assert_eq!(Some(5), decoded.header.packet_count);
        assert_eq!(b"OPUSRAW1\x00\x03abc", decoded.payload.as_slice());
    }

    #[test]
    fn rejects_invalid_binary_audio_frame_header_size() {
        let mut frame = Vec::new();
        frame.extend_from_slice(super::AUDIO_FRAME_MAGIC);
        frame.extend_from_slice(&((AUDIO_FRAME_HEADER_MAX_BYTES as u32) + 1).to_be_bytes());
        frame.extend_from_slice(b"{}");
        frame.extend_from_slice(b"payload");

        assert!(decode_audio_frame(&frame).is_err());
    }
}
