#![allow(dead_code)]

#[path = "../audio/mod.rs"]
mod audio;
#[path = "../audio_control.rs"]
mod audio_control;
#[path = "../config.rs"]
mod config;
#[path = "../gateway.rs"]
mod gateway;
#[path = "../logging.rs"]
mod logging;
#[path = "../protocol.rs"]
mod protocol;
#[cfg(feature = "native-webrtc")]
#[path = "../snapshot.rs"]
mod snapshot;
#[path = "../transport/mod.rs"]
mod transport;

use anyhow::{bail, Context, Result};
use audio::opus_codec::{
    encode_pcm16_opus_packet_stream, OpusStreamDecoder, OPUS_CHANNELS, OPUS_FRAME_DURATION_MS,
    OPUS_SAMPLE_RATE,
};
use protocol::{AudioFrameHeader, ClientMessage, ServerMessage};
use serde::Serialize;
use std::{
    env,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::{sync::mpsc, time};
use transport::{
    hybrid::{HybridConnectOptions, HybridTransport},
    TransportEvent, TransportPolicy, VoiceTransport, WebRtcAudioUplinkMode,
};

const DEFAULT_GATEWAY_URL: &str = "ws://127.0.0.1:8282/ws";
const DEFAULT_ROBOT_ID: &str = "companion_01";
const DEFAULT_BOT_ID: &str = "xiaowen";
const DEFAULT_CONNECT_TIMEOUT_MS: u64 = 15_000;
const DEFAULT_WRITE_TIMEOUT_MS: u64 = 5_000;
const DEFAULT_DONE_TIMEOUT_MS: u64 = 90_000;
const DEFAULT_PRE_AUDIO_WAIT_MS: u64 = 1_000;
const DEFAULT_CHUNK_MS: u64 = 100;
const DEFAULT_ROUNDS: u64 = 1;
const DEFAULT_INTER_ROUND_GAP_MS: u64 = 500;
const DONE_AUDIO_DRAIN_QUIET_MS: u64 = 250;

#[derive(Debug)]
struct SmokeConfig {
    gateway_url: String,
    robot_id: String,
    robot_secret: Option<String>,
    bot_id: String,
    sample_path: PathBuf,
    barge_in_sample_path: Option<PathBuf>,
    utterance_id: String,
    transport_policy: TransportPolicy,
    webrtc_enabled: bool,
    fallback_enabled: bool,
    audio_uplink_mode: WebRtcAudioUplinkMode,
    connect_timeout: Duration,
    write_timeout: Duration,
    webrtc_connect_timeout: Duration,
    done_timeout: Duration,
    pre_audio_wait: Duration,
    chunk_ms: u64,
    realtime_gap: bool,
    downlink_wav_path: Option<PathBuf>,
    rounds: u64,
    inter_round_gap: Duration,
    quiet_audio_frames: bool,
    interrupt_after_playback: Option<Duration>,
    natural_barge_in_after_playback: Option<Duration>,
    interrupt_via_client_event: bool,
    old_playback_report_delay: Duration,
    strict_wake_interrupt: bool,
    vision_fixture_path: Option<PathBuf>,
    direct_text: Option<String>,
    vision_settle: Duration,
    post_rounds_hold: Duration,
}

#[derive(Debug, Serialize)]
struct RoundReport {
    round: u64,
    utterance_id: String,
    response_asr_count: u64,
    valid_asr_count: u64,
    text_count: u64,
    audio_frame_count: u64,
    stale_audio_frame_count: u64,
    asr_ms: Option<f64>,
    playback_start_ms: Option<f64>,
    first_tts_audio_ms: Option<f64>,
    interrupt_sent_ms: Option<f64>,
    playback_cancel_ms: Option<f64>,
    replacement_playback_start_ms: Option<f64>,
    initial_rtp_start_sequence: Option<u16>,
    replacement_rtp_start_sequence: Option<u16>,
    barge_in_decision: Option<String>,
    barge_in_asr_text: Option<String>,
    replacement_audio_frame_count: u64,
    done_ms: Option<f64>,
    playback_complete_ms: f64,
}

#[derive(Debug, Serialize)]
struct StabilityReport {
    robot_id: String,
    rounds_requested: u64,
    rounds_completed: u64,
    valid_asr_rounds: u64,
    tts_audio_rounds: u64,
    reports: Vec<RoundReport>,
}

struct ReceiveDownlinkOptions<'a> {
    timeout: Duration,
    downlink_wav_path: Option<&'a Path>,
    round: u64,
    utterance_id: String,
    quiet_audio_frames: bool,
    interrupt_after_playback: Option<Duration>,
    natural_barge_in_after_playback: Option<Duration>,
    barge_in_samples: &'a [i16],
    chunk_ms: u64,
    bot_id: &'a str,
    interrupt_via_client_event: bool,
    old_playback_report_delay: Duration,
    strict_wake_interrupt: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    logging::init_tracing();
    let cfg = SmokeConfig::from_env()?;
    run(cfg).await
}

async fn run(cfg: SmokeConfig) -> Result<()> {
    let samples = if cfg.direct_text.is_some() {
        Vec::new()
    } else {
        load_wav_mono_16k(&cfg.sample_path)?
    };
    let barge_in_samples = match cfg.barge_in_sample_path.as_deref() {
        Some(path) => {
            let loaded = load_wav_mono_16k(path)?;
            println!(
                "barge_in_sample={} samples={} duration_ms={:.0}",
                path.display(),
                loaded.len(),
                loaded.len() as f64 * 1000.0 / OPUS_SAMPLE_RATE as f64
            );
            loaded
        }
        None => samples.clone(),
    };
    if !samples.is_empty() {
        println!(
            "sample={} samples={} duration_ms={:.0}",
            cfg.sample_path.display(),
            samples.len(),
            samples.len() as f64 * 1000.0 / OPUS_SAMPLE_RATE as f64
        );
    }
    println!(
        "transport_policy={} rtc_audio_uplink={}",
        cfg.transport_policy.as_str(),
        cfg.audio_uplink_mode.as_str()
    );

    let vision = config::VisionConfig {
        enabled: env_bool("VISION_SNAPSHOT_ENABLED", false),
        snapshot_path: env_or("VISION_SNAPSHOT_PATH", "temp/pic.jpeg"),
        scan_interval_ms: env_u64("VISION_SNAPSHOT_SCAN_INTERVAL_MS", 500).max(100),
    };
    let (conn, mut events) = HybridTransport::connect(HybridConnectOptions {
        url: &cfg.gateway_url,
        connect_timeout: cfg.connect_timeout,
        write_timeout: cfg.write_timeout,
        policy: cfg.transport_policy,
        webrtc_enabled: cfg.webrtc_enabled,
        webrtc_connect_timeout: cfg.webrtc_connect_timeout,
        fallback_enabled: cfg.fallback_enabled,
        webrtc_audio_uplink_mode: cfg.audio_uplink_mode,
        vision: &vision,
    })
    .await?;

    conn.send_control(&ClientMessage::Register {
        robot_id: cfg.robot_id.clone(),
        robot_secret: cfg.robot_secret.clone(),
        client_type: "rust_live_smoke".to_string(),
    })
    .await?;

    wait_for_rtc_answer(&mut events, cfg.connect_timeout).await?;
    wait_for_audio_uplink_ready(&conn, cfg.webrtc_connect_timeout).await?;
    if !cfg.pre_audio_wait.is_zero() {
        time::sleep(cfg.pre_audio_wait).await;
    }

    let mut reports = Vec::with_capacity(cfg.rounds as usize);
    for round in 1..=cfg.rounds {
        inject_vision_fixture(&cfg)?;
        if cfg.vision_fixture_path.is_some() && !cfg.vision_settle.is_zero() {
            time::sleep(cfg.vision_settle).await;
        }
        let utterance_id = if cfg.rounds == 1 {
            cfg.utterance_id.clone()
        } else {
            format!("{}-{round:03}", cfg.utterance_id)
        };
        let downlink_wav_path =
            round_downlink_path(cfg.downlink_wav_path.as_deref(), round, cfg.rounds);
        if let Some(content) = cfg.direct_text.as_deref() {
            let trace_id = format!("vision-{utterance_id}");
            conn.send_control(&ClientMessage::Text {
                content: content.to_string(),
                bot_id: cfg.bot_id.clone(),
                source: "rust_live_smoke".to_string(),
                trace_id: Some(trace_id.clone()),
            })
            .await?;
            println!(
                "direct_text_sent trace_id={trace_id} chars={}",
                content.chars().count()
            );
        } else {
            send_sample_audio(&cfg, &conn, &samples, &utterance_id).await?;
        }
        reports.push(
            receive_downlink_until_done(
                &conn,
                &mut events,
                ReceiveDownlinkOptions {
                    timeout: cfg.done_timeout,
                    downlink_wav_path: downlink_wav_path.as_deref(),
                    round,
                    utterance_id,
                    quiet_audio_frames: cfg.quiet_audio_frames,
                    interrupt_after_playback: cfg.interrupt_after_playback,
                    natural_barge_in_after_playback: cfg.natural_barge_in_after_playback,
                    barge_in_samples: &barge_in_samples,
                    chunk_ms: cfg.chunk_ms,
                    bot_id: &cfg.bot_id,
                    interrupt_via_client_event: cfg.interrupt_via_client_event,
                    old_playback_report_delay: cfg.old_playback_report_delay,
                    strict_wake_interrupt: cfg.strict_wake_interrupt,
                },
            )
            .await?,
        );
        if round < cfg.rounds && !cfg.inter_round_gap.is_zero() {
            time::sleep(cfg.inter_round_gap).await;
        }
    }

    let report = StabilityReport {
        robot_id: cfg.robot_id,
        rounds_requested: cfg.rounds,
        rounds_completed: reports.len() as u64,
        valid_asr_rounds: reports
            .iter()
            .filter(|report| report.valid_asr_count > 0)
            .count() as u64,
        tts_audio_rounds: reports
            .iter()
            .filter(|report| report.audio_frame_count > 0)
            .count() as u64,
        reports,
    };
    println!(
        "stability_report={}",
        serde_json::to_string(&report).context("serialize stability report")?
    );
    if !cfg.post_rounds_hold.is_zero() {
        println!("post_rounds_hold_ms={}", cfg.post_rounds_hold.as_millis());
        time::sleep(cfg.post_rounds_hold).await;
    }
    Ok(())
}

async fn wait_for_rtc_answer(
    events: &mut mpsc::Receiver<TransportEvent>,
    timeout: Duration,
) -> Result<()> {
    let deadline = time::Instant::now() + timeout;
    let mut registered = false;
    let mut got_rtc_answer = false;

    while time::Instant::now() < deadline {
        let event = next_event(events, deadline).await?;
        match event {
            TransportEvent::Message(ServerMessage::Registered {
                session_id,
                robot_id,
                bot_id,
                ..
            }) => {
                registered = true;
                println!("registered session={session_id} robot_id={robot_id} bot_id={bot_id}");
            }
            TransportEvent::Message(ServerMessage::RtcConfig(config)) => {
                println!(
                    "rtc_config session={} ice_servers={}",
                    config.session_id,
                    config.ice_servers.len()
                );
            }
            TransportEvent::Message(ServerMessage::RtcAnswer(answer)) => {
                got_rtc_answer = true;
                println!(
                    "rtc_answer session={} sdp_bytes={}",
                    answer.session_id,
                    answer.sdp.len()
                );
            }
            TransportEvent::Message(ServerMessage::TransportFallbackAck(ack)) => {
                bail!(
                    "unexpected WebRTC fallback ack session={} active_transport={} reason={}",
                    ack.session_id,
                    ack.active_transport,
                    ack.reason.unwrap_or_default()
                );
            }
            TransportEvent::Message(ServerMessage::Error { code, message }) => {
                bail!("gateway error before audio code={code} message={message}");
            }
            TransportEvent::Message(ServerMessage::Connected { session_id }) => {
                println!("connected session={session_id}");
            }
            TransportEvent::Message(ServerMessage::RtcIceCandidate(_)) => {}
            TransportEvent::Message(other) => {
                println!("pre_audio_json_type={}", server_message_type(&other));
            }
            TransportEvent::AudioFrame(frame) => {
                println!("pre_audio_frame_payload_bytes={}", frame.payload.len());
            }
            TransportEvent::Closed => bail!("transport closed before rtc_answer"),
            TransportEvent::Error(err) => return Err(err).context("transport error before audio"),
        }

        if registered && got_rtc_answer {
            return Ok(());
        }
    }

    bail!(
        "timeout waiting for registered+rtc_answer registered={registered} rtc_answer={got_rtc_answer}"
    )
}

async fn wait_for_audio_uplink_ready(conn: &HybridTransport, timeout: Duration) -> Result<()> {
    let started_at = Instant::now();
    let deadline = time::Instant::now() + timeout;
    while time::Instant::now() < deadline {
        if conn.audio_uplink_ready() {
            println!(
                "audio_uplink_ready_ms={:.0}",
                started_at.elapsed().as_secs_f64() * 1000.0
            );
            return Ok(());
        }
        time::sleep(Duration::from_millis(50)).await;
    }
    bail!(
        "timeout waiting for native RTP audio uplink ready after {}ms",
        timeout.as_millis()
    )
}

async fn send_sample_audio(
    cfg: &SmokeConfig,
    conn: &HybridTransport,
    samples: &[i16],
    utterance_id: &str,
) -> Result<()> {
    let chunk_samples =
        ((OPUS_SAMPLE_RATE as u64 * cfg.chunk_ms) / 1000).max(1) as usize * OPUS_CHANNELS as usize;
    let total_chunks = samples.len().div_ceil(chunk_samples);

    conn.send_control(&ClientMessage::AudioStart {
        bot_id: cfg.bot_id.clone(),
        trace_id: Some(format!("audio-{utterance_id}")),
        utterance_id: utterance_id.to_string(),
        sample_rate: OPUS_SAMPLE_RATE,
        channels: OPUS_CHANNELS,
        opus_frame_ms: OPUS_FRAME_DURATION_MS,
    })
    .await?;

    let mut total_packets = 0u64;
    let mut total_opus_bytes = 0u64;
    for (chunk_index, chunk) in samples.chunks(chunk_samples).enumerate() {
        let opus = encode_pcm16_opus_packet_stream(chunk, OPUS_SAMPLE_RATE, OPUS_CHANNELS)
            .with_context(|| format!("encode Opus chunk {}", chunk_index + 1))?;
        let duration_ms = chunk.len() as f64 * 1000.0 / OPUS_SAMPLE_RATE as f64;
        let header = AudioFrameHeader::client_input_opus_chunk(
            cfg.bot_id.clone(),
            Some(format!("audio-{utterance_id}")),
            utterance_id.to_string(),
            chunk_index as u64 + 1,
            OPUS_SAMPLE_RATE,
            OPUS_CHANNELS,
            duration_ms,
            opus.frame_duration_ms,
            opus.packet_count,
        );
        conn.send_audio_frame(&header, &opus.payload)
            .await
            .with_context(|| format!("send audio chunk {}", chunk_index + 1))?;
        total_packets += opus.packet_count as u64;
        total_opus_bytes += opus.payload.len() as u64;
        if cfg.realtime_gap {
            time::sleep(Duration::from_millis(cfg.chunk_ms)).await;
        }
    }

    let duration_ms = samples.len() as f64 * 1000.0 / OPUS_SAMPLE_RATE as f64;
    conn.send_control(&ClientMessage::AudioEnd {
        bot_id: cfg.bot_id.clone(),
        trace_id: Some(format!("audio-{utterance_id}")),
        utterance_id: utterance_id.to_string(),
        duration_ms,
        samples: samples.len() as u64,
        chunks: total_chunks as u64,
        packets: total_packets,
        pcm_bytes: std::mem::size_of_val(samples) as u64,
        opus_bytes: total_opus_bytes,
    })
    .await?;

    println!(
        "audio_sent utterance_id={} chunks={} packets={} samples={} opus_bytes={}",
        utterance_id,
        total_chunks,
        total_packets,
        samples.len(),
        total_opus_bytes
    );
    Ok(())
}

async fn send_barge_in_probe_audio(
    conn: &HybridTransport,
    samples: &[i16],
    bot_id: &str,
    chunk_ms: u64,
    round_id: &str,
    playback_id: &str,
) -> Result<(String, String)> {
    anyhow::ensure!(
        samples.len() >= (OPUS_SAMPLE_RATE as usize * 800 / 1_000),
        "barge-in smoke sample must contain at least 800ms"
    );
    let utterance_id = format!("barge-live-{}", unix_nanos());
    let trace_id = format!("audio-{utterance_id}");
    conn.send_control(&ClientMessage::BargeInStart {
        bot_id: bot_id.to_string(),
        trace_id: Some(trace_id.clone()),
        utterance_id: utterance_id.clone(),
        round_id: round_id.to_string(),
        playback_id: playback_id.to_string(),
        speech_epoch: 1,
        sample_rate: OPUS_SAMPLE_RATE,
        channels: OPUS_CHANNELS,
        opus_frame_ms: OPUS_FRAME_DURATION_MS,
    })
    .await?;

    let chunk_samples =
        ((OPUS_SAMPLE_RATE as u64 * chunk_ms) / 1000).max(1) as usize * OPUS_CHANNELS as usize;
    for (chunk_index, chunk) in samples.chunks(chunk_samples).enumerate() {
        let opus = encode_pcm16_opus_packet_stream(chunk, OPUS_SAMPLE_RATE, OPUS_CHANNELS)
            .with_context(|| format!("encode barge-in Opus chunk {}", chunk_index + 1))?;
        let duration_ms = chunk.len() as f64 * 1000.0 / OPUS_SAMPLE_RATE as f64;
        let header = AudioFrameHeader::client_input_opus_chunk(
            bot_id.to_string(),
            Some(trace_id.clone()),
            utterance_id.clone(),
            chunk_index as u64 + 1,
            OPUS_SAMPLE_RATE,
            OPUS_CHANNELS,
            duration_ms,
            opus.frame_duration_ms,
            opus.packet_count,
        );
        conn.send_audio_frame(&header, &opus.payload).await?;
    }
    conn.send_control(&ClientMessage::BargeInProbe {
        bot_id: bot_id.to_string(),
        trace_id: Some(trace_id.clone()),
        utterance_id: utterance_id.clone(),
        round_id: round_id.to_string(),
        playback_id: playback_id.to_string(),
        candidate_seq: 1,
        speech_epoch: 1,
        audio_watermark: samples.len() as u64,
    })
    .await?;
    println!(
        "barge_in_probe_sent utterance_id={} round_id={} playback_id={} samples={}",
        utterance_id,
        round_id,
        playback_id,
        samples.len()
    );
    Ok((trace_id, utterance_id))
}

async fn receive_downlink_until_done(
    conn: &HybridTransport,
    events: &mut mpsc::Receiver<TransportEvent>,
    options: ReceiveDownlinkOptions<'_>,
) -> Result<RoundReport> {
    let ReceiveDownlinkOptions {
        timeout,
        downlink_wav_path,
        round,
        utterance_id,
        quiet_audio_frames,
        interrupt_after_playback,
        natural_barge_in_after_playback,
        barge_in_samples,
        chunk_ms,
        bot_id,
        interrupt_via_client_event,
        old_playback_report_delay,
        strict_wake_interrupt,
    } = options;
    let started_at = Instant::now();
    let deadline = time::Instant::now() + timeout;
    let mut text_count = 0u64;
    let mut response_asr_count = 0u64;
    let mut valid_asr_count = 0u64;
    let mut audio_frame_count = 0u64;
    let mut stale_audio_frame_count = 0u64;
    let mut asr_ms = None;
    let mut playback_start_ms = None;
    let mut first_tts_audio_ms = None;
    let mut playback_round_id: Option<String> = None;
    let mut playback_id: Option<String> = None;
    let mut done_ms = None;
    let mut interrupt_sent_ms = None;
    let mut playback_cancel_ms = None;
    let mut replacement_playback_start_ms = None;
    let mut initial_rtp_start_sequence = None;
    let mut replacement_rtp_start_sequence = None;
    let mut barge_in_identity: Option<(String, String, String)> = None;
    let mut barge_in_decision: Option<String> = None;
    let mut barge_in_asr_text: Option<String> = None;
    let mut replacement_audio_frame_count = 0u64;
    let mut decoder: Option<OpusStreamDecoder> = None;
    let mut downlink_samples = Vec::new();

    while time::Instant::now() < deadline {
        let completion_observed = if natural_barge_in_after_playback.is_some() {
            barge_in_decision.as_deref() == Some("new_intent")
                && playback_cancel_ms.is_some()
                && replacement_playback_start_ms.is_some()
                && replacement_audio_frame_count > 0
                && done_ms.is_some()
        } else if interrupt_after_playback.is_some() && !interrupt_via_client_event {
            playback_cancel_ms.is_some()
        } else {
            done_ms.is_some()
        };
        let event = if completion_observed {
            match time::timeout(
                Duration::from_millis(DONE_AUDIO_DRAIN_QUIET_MS),
                events.recv(),
            )
            .await
            {
                Ok(Some(event)) => event,
                Ok(None) => bail!("transport event channel closed after done"),
                Err(_) => break,
            }
        } else {
            next_event(events, deadline).await?
        };
        match event {
            TransportEvent::Message(ServerMessage::Status { message, .. }) => {
                println!("status={message}");
            }
            TransportEvent::Message(ServerMessage::Text { content, .. }) => {
                text_count += 1;
                println!("text={content}");
            }
            TransportEvent::Message(ServerMessage::ResponseAsr { payload, .. }) => {
                response_asr_count += 1;
                if payload.valid {
                    valid_asr_count += 1;
                }
                asr_ms.get_or_insert_with(|| started_at.elapsed().as_secs_f64() * 1000.0);
                println!(
                    "json_type=response.asr valid={} final={} asr_time_ms={:?} text={}",
                    payload.valid, payload.final_result, payload.asr_time_ms, payload.text
                );
            }
            TransportEvent::Message(ServerMessage::BargeInDecision {
                trace_id,
                utterance_id: candidate_utterance_id,
                round_id,
                playback_id: candidate_playback_id,
                candidate_seq,
                speech_epoch,
                audio_watermark,
                decision,
                reason,
                asr_text,
                ..
            }) => {
                let expected = barge_in_identity
                    .as_ref()
                    .context("received barge-in decision without active live candidate")?;
                anyhow::ensure!(
                    expected.0 == candidate_utterance_id
                        && expected.1 == round_id
                        && expected.2 == candidate_playback_id
                        && candidate_seq == 1
                        && speech_epoch == 1
                        && audio_watermark == barge_in_samples.len() as u64,
                    "barge-in decision identity mismatch"
                );
                anyhow::ensure!(
                    decision == "new_intent",
                    "barge-in live smoke expected new_intent, got {decision} reason={}",
                    reason.as_deref().unwrap_or("-")
                );
                conn.send_control(&ClientMessage::Interrupt {
                    reason: Some("natural_barge_in".to_string()),
                })
                .await?;
                conn.send_control(&ClientMessage::BargeInCommitAck {
                    bot_id: bot_id.to_string(),
                    trace_id,
                    utterance_id: candidate_utterance_id.clone(),
                    round_id,
                    playback_id: candidate_playback_id,
                    candidate_seq,
                    speech_epoch,
                    audio_watermark,
                    accepted: true,
                    reason: "live_smoke_committed".to_string(),
                })
                .await?;
                interrupt_sent_ms
                    .get_or_insert_with(|| started_at.elapsed().as_secs_f64() * 1000.0);
                barge_in_decision = Some(decision.clone());
                barge_in_asr_text = asr_text.clone();
                println!(
                    "barge_in_decision={} asr_text={} reason={}",
                    decision,
                    asr_text.as_deref().unwrap_or("-"),
                    reason.as_deref().unwrap_or("-")
                );
            }
            TransportEvent::Message(ServerMessage::PlaybackStart {
                round_id,
                playback_id: next_playback_id,
                rtp_start_sequence,
                ..
            }) => {
                let playback_started_ms = started_at.elapsed().as_secs_f64() * 1000.0;
                if interrupt_sent_ms.is_some() {
                    replacement_playback_start_ms.get_or_insert(playback_started_ms);
                    replacement_rtp_start_sequence = rtp_start_sequence;
                } else {
                    playback_start_ms.get_or_insert(playback_started_ms);
                    initial_rtp_start_sequence = rtp_start_sequence;
                }
                playback_round_id = Some(round_id);
                playback_id = Some(next_playback_id);
                println!(
                    "json_type=playback_start rtp_start_sequence={}",
                    rtp_start_sequence
                        .map(|value| value.to_string())
                        .unwrap_or_else(|| "-".to_string())
                );
                if let Some(delay) = natural_barge_in_after_playback
                    .filter(|_| interrupt_sent_ms.is_none() && barge_in_identity.is_none())
                {
                    if !delay.is_zero() {
                        time::sleep(delay).await;
                    }
                    let (_, candidate_utterance_id) = send_barge_in_probe_audio(
                        conn,
                        barge_in_samples,
                        bot_id,
                        chunk_ms,
                        playback_round_id.as_deref().unwrap_or_default(),
                        playback_id.as_deref().unwrap_or_default(),
                    )
                    .await?;
                    barge_in_identity = Some((
                        candidate_utterance_id,
                        playback_round_id.clone().unwrap_or_default(),
                        playback_id.clone().unwrap_or_default(),
                    ));
                }
                if let Some(delay) =
                    interrupt_after_playback.filter(|_| interrupt_sent_ms.is_none())
                {
                    if !delay.is_zero() {
                        time::sleep(delay).await;
                    }
                    if interrupt_via_client_event {
                        let event_id = format!("stability-wake-interrupt-{}", unix_nanos());
                        conn.send_control(&ClientMessage::ClientEvent {
                            event: "wake_interrupt".to_string(),
                            bot_id: bot_id.to_string(),
                            source: "rust_live_smoke".to_string(),
                            trace_id: Some(event_id.clone()),
                            event_id: Some(event_id),
                        })
                        .await?;
                    } else {
                        conn.send_control(&ClientMessage::Interrupt { reason: None })
                            .await?;
                    }
                    interrupt_sent_ms
                        .get_or_insert_with(|| started_at.elapsed().as_secs_f64() * 1000.0);
                    println!(
                        "client_event={}",
                        if interrupt_via_client_event {
                            "wake_interrupt"
                        } else {
                            "interrupt"
                        }
                    );
                    if interrupt_via_client_event {
                        if let (Some(round_id), Some(playback_id)) =
                            (playback_round_id.take(), playback_id.take())
                        {
                            if !old_playback_report_delay.is_zero() {
                                time::sleep(old_playback_report_delay).await;
                            }
                            conn.send_control(&ClientMessage::PlaybackInterrupted {
                                trace_id: Some(round_id.clone()),
                                round_id,
                                playback_id,
                                reason: Some("wake_interrupt".to_string()),
                                first_audio_to_playback_start_ms: None,
                                playback_start_to_complete_ms: playback_start_ms.map(|started| {
                                    started_at.elapsed().as_secs_f64() * 1000.0 - started
                                }),
                                pushed_chunks: audio_frame_count,
                                pushed_samples: 0,
                                underrun_callbacks: 0,
                                zero_filled_samples: 0,
                                max_buffered_samples: 0,
                            })
                            .await?;
                            println!(
                                "delayed_old_playback_interrupted_ms={}",
                                old_playback_report_delay.as_millis()
                            );
                        }
                    }
                }
            }
            TransportEvent::Message(ServerMessage::PlaybackCancel { reason, .. }) => {
                playback_cancel_ms
                    .get_or_insert_with(|| started_at.elapsed().as_secs_f64() * 1000.0);
                if natural_barge_in_after_playback.is_some() {
                    playback_round_id = None;
                    playback_id = None;
                }
                println!(
                    "json_type=playback_cancel reason={}",
                    reason.as_deref().unwrap_or("-")
                );
            }
            TransportEvent::Message(ServerMessage::Done { round_id, .. }) => {
                if natural_barge_in_after_playback.is_some()
                    && replacement_playback_start_ms.is_none()
                {
                    println!(
                        "ignored_initial_done_round={}",
                        round_id.as_deref().unwrap_or("-")
                    );
                    continue;
                }
                let matches_current = match playback_round_id.as_deref() {
                    Some(current) => round_id
                        .as_deref()
                        .is_none_or(|done_round| done_round == current),
                    None => interrupt_sent_ms.is_none(),
                };
                if matches_current {
                    done_ms.get_or_insert_with(|| started_at.elapsed().as_secs_f64() * 1000.0);
                } else {
                    println!("ignored_done_round={}", round_id.as_deref().unwrap_or("-"));
                }
            }
            TransportEvent::Message(ServerMessage::Error { code, message }) => {
                bail!("gateway error code={code} message={message}");
            }
            TransportEvent::Message(ServerMessage::RtcIceCandidate(_)) => {}
            TransportEvent::Message(ServerMessage::HeartbeatAck) => {
                println!("json_type=heartbeat_ack");
            }
            TransportEvent::Message(message) => {
                println!("json_type={}", server_message_type(&message));
            }
            TransportEvent::AudioFrame(frame) => {
                if interrupt_sent_ms.is_some() && playback_round_id.is_none() {
                    stale_audio_frame_count += 1;
                    continue;
                }
                let matches_current_round =
                    match playback_round_id.as_deref() {
                        Some(current_round_id) => {
                            frame.header.round_id.as_deref().is_none_or(|round_id| {
                                round_id == current_round_id
                                    && frame.header.playback_id.as_deref().is_none_or(|frame_id| {
                                        playback_id.as_deref() == Some(frame_id)
                                    })
                            })
                        }
                        None => false,
                    };
                if !matches_current_round {
                    stale_audio_frame_count += 1;
                    continue;
                }
                audio_frame_count += 1;
                if replacement_playback_start_ms.is_some() {
                    replacement_audio_frame_count += 1;
                }
                first_tts_audio_ms
                    .get_or_insert_with(|| started_at.elapsed().as_secs_f64() * 1000.0);
                if downlink_wav_path.is_some() && frame.header.encoding.eq_ignore_ascii_case("opus")
                {
                    let frame_decoder = match decoder.as_mut() {
                        Some(decoder) => decoder,
                        None => decoder.insert(OpusStreamDecoder::new(
                            frame.header.sample_rate.unwrap_or(OPUS_SAMPLE_RATE),
                            frame.header.channels.unwrap_or(OPUS_CHANNELS),
                            frame.header.opus_frame_ms.unwrap_or(OPUS_FRAME_DURATION_MS),
                        )?),
                    };
                    downlink_samples.extend(
                        frame_decoder
                            .decode_packet_stream(&frame.payload, frame.header.duration_ms)?,
                    );
                }
                if !quiet_audio_frames {
                    println!(
                        "audio_frame_payload_bytes={} encoding={} direction={} duration_ms={}",
                        frame.payload.len(),
                        frame.header.encoding,
                        frame.header.direction.as_deref().unwrap_or("-"),
                        frame.header.duration_ms.unwrap_or_default()
                    );
                }
            }
            TransportEvent::Closed => bail!("transport closed before done"),
            TransportEvent::Error(err) => return Err(err).context("transport error after audio"),
        }
    }

    if natural_barge_in_after_playback.is_some() {
        anyhow::ensure!(
            barge_in_decision.as_deref() == Some("new_intent"),
            "natural barge-in: expected new_intent decision"
        );
        playback_cancel_ms.context("natural barge-in: old playback was not cancelled")?;
        replacement_playback_start_ms
            .context("natural barge-in: replacement playback_start was not received")?;
        replacement_rtp_start_sequence
            .context("natural barge-in: replacement playback_start lacks rtp_start_sequence")?;
        anyhow::ensure!(
            replacement_audio_frame_count > 0,
            "natural barge-in: replacement playback produced no accepted RTP audio"
        );
        done_ms.context("natural barge-in: replacement playback did not finish")?;
    } else if interrupt_after_playback.is_some() && !interrupt_via_client_event {
        playback_cancel_ms.context("timeout waiting for playback_cancel")?;
    } else {
        done_ms.context("timeout waiting for downlink done")?;
    }
    if strict_wake_interrupt {
        if !interrupt_via_client_event || interrupt_after_playback.is_none() {
            bail!(
                "strict wake interrupt requires RUST_LIVE_SMOKE_INTERRUPT_AFTER_PLAYBACK_MS \
                 and RUST_LIVE_SMOKE_INTERRUPT_VIA_CLIENT_EVENT=true"
            );
        }
        if valid_asr_count == 0 {
            bail!("strict wake interrupt: initial speech did not produce valid ASR");
        }
        initial_rtp_start_sequence
            .context("strict wake interrupt: initial playback_start lacks rtp_start_sequence")?;
        interrupt_sent_ms.context("strict wake interrupt: wake_interrupt was not sent")?;
        replacement_playback_start_ms
            .context("strict wake interrupt: replacement playback_start was not received")?;
        replacement_rtp_start_sequence.context(
            "strict wake interrupt: replacement playback_start lacks rtp_start_sequence",
        )?;
        if audio_frame_count == 0 {
            bail!("strict wake interrupt: replacement playback produced no accepted RTP audio");
        }
    }
    if let Some(path) = downlink_wav_path {
        write_wav_mono_16k(path, &downlink_samples)?;
        println!(
            "downlink_wav={} samples={} duration_ms={:.0}",
            path.display(),
            downlink_samples.len(),
            downlink_samples.len() as f64 * 1000.0 / OPUS_SAMPLE_RATE as f64
        );
    }
    if let (Some(round_id), Some(playback_id)) =
        (playback_round_id.as_deref(), playback_id.as_deref())
    {
        let playback_start_to_complete_ms =
            playback_start_ms.map(|started| started_at.elapsed().as_secs_f64() * 1000.0 - started);
        let report = if interrupt_after_playback.is_some() && !interrupt_via_client_event {
            ClientMessage::PlaybackInterrupted {
                trace_id: Some(round_id.to_string()),
                round_id: round_id.to_string(),
                playback_id: playback_id.to_string(),
                reason: Some("stability_test_interrupt".to_string()),
                first_audio_to_playback_start_ms: None,
                playback_start_to_complete_ms,
                pushed_chunks: audio_frame_count,
                pushed_samples: 0,
                underrun_callbacks: 0,
                zero_filled_samples: 0,
                max_buffered_samples: 0,
            }
        } else {
            ClientMessage::PlaybackComplete {
                trace_id: Some(round_id.to_string()),
                round_id: round_id.to_string(),
                playback_id: playback_id.to_string(),
                first_audio_to_playback_start_ms: None,
                playback_start_to_complete_ms,
                pushed_chunks: audio_frame_count,
                pushed_samples: 0,
                underrun_callbacks: 0,
                zero_filled_samples: 0,
                max_buffered_samples: 0,
            }
        };
        conn.send_control(&report).await?;
    }
    println!(
        "done=true text_count={text_count} audio_frame_count={audio_frame_count} stale_audio_frame_count={stale_audio_frame_count}"
    );
    Ok(RoundReport {
        round,
        utterance_id,
        response_asr_count,
        valid_asr_count,
        text_count,
        audio_frame_count,
        stale_audio_frame_count,
        asr_ms,
        playback_start_ms,
        first_tts_audio_ms,
        interrupt_sent_ms,
        playback_cancel_ms,
        replacement_playback_start_ms,
        initial_rtp_start_sequence,
        replacement_rtp_start_sequence,
        barge_in_decision,
        barge_in_asr_text,
        replacement_audio_frame_count,
        done_ms,
        playback_complete_ms: started_at.elapsed().as_secs_f64() * 1000.0,
    })
}

async fn next_event(
    events: &mut mpsc::Receiver<TransportEvent>,
    deadline: time::Instant,
) -> Result<TransportEvent> {
    let now = time::Instant::now();
    if now >= deadline {
        bail!("timeout waiting for transport event");
    }
    match time::timeout(deadline - now, events.recv()).await {
        Ok(Some(event)) => Ok(event),
        Ok(None) => bail!("transport event channel closed"),
        Err(_) => bail!("timeout waiting for transport event"),
    }
}

fn load_wav_mono_16k(path: &Path) -> Result<Vec<i16>> {
    let mut reader = hound::WavReader::open(path)
        .with_context(|| format!("open WAV sample failed: {}", path.display()))?;
    let spec = reader.spec();
    if spec.sample_format != hound::SampleFormat::Int
        || spec.bits_per_sample != 16
        || spec.channels != OPUS_CHANNELS
        || spec.sample_rate != OPUS_SAMPLE_RATE
    {
        bail!(
            "sample must be mono 16-bit PCM WAV at {}Hz: {} ({:?}, bits={}, channels={}, rate={})",
            OPUS_SAMPLE_RATE,
            path.display(),
            spec.sample_format,
            spec.bits_per_sample,
            spec.channels,
            spec.sample_rate
        );
    }
    let samples = reader
        .samples::<i16>()
        .collect::<std::result::Result<Vec<_>, _>>()
        .with_context(|| format!("read WAV samples failed: {}", path.display()))?;
    if samples.is_empty() {
        bail!("sample WAV is empty: {}", path.display());
    }
    Ok(samples)
}

fn write_wav_mono_16k(path: &Path, samples: &[i16]) -> Result<()> {
    if samples.is_empty() {
        bail!("downlink WAV has no decoded samples");
    }
    let spec = hound::WavSpec {
        channels: OPUS_CHANNELS,
        sample_rate: OPUS_SAMPLE_RATE,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(path, spec)
        .with_context(|| format!("create downlink WAV failed: {}", path.display()))?;
    for sample in samples {
        writer.write_sample(*sample)?;
    }
    writer.finalize()?;
    Ok(())
}

impl SmokeConfig {
    fn from_env() -> Result<Self> {
        let sample_path = env_non_empty("RUST_LIVE_SMOKE_SAMPLE")
            .map(PathBuf::from)
            .context("RUST_LIVE_SMOKE_SAMPLE is required")?;
        let utterance_id = env_non_empty("RUST_LIVE_SMOKE_UTTERANCE_ID")
            .unwrap_or_else(|| format!("rust-live-{}", unix_nanos()));
        let transport_policy = TransportPolicy::parse(&env_or(
            "TRANSPORT_POLICY",
            TransportPolicy::WebRtcOnly.as_str(),
        ))?;
        let audio_uplink_mode =
            WebRtcAudioUplinkMode::parse(&env_or("RTC_AUDIO_UPLINK", "rtp_only"))?;
        Ok(Self {
            gateway_url: env_or("GATEWAY_URL", DEFAULT_GATEWAY_URL),
            robot_id: env_or("ROBOT_ID", DEFAULT_ROBOT_ID),
            robot_secret: env_non_empty("ROBOT_SECRET"),
            bot_id: env_or("BOT_ID", DEFAULT_BOT_ID),
            sample_path,
            barge_in_sample_path: env_non_empty("RUST_LIVE_SMOKE_BARGE_IN_SAMPLE")
                .map(PathBuf::from),
            utterance_id,
            transport_policy,
            webrtc_enabled: env_bool("WEBRTC_ENABLED", true),
            fallback_enabled: env_bool("TRANSPORT_FALLBACK_ENABLED", false),
            audio_uplink_mode,
            connect_timeout: Duration::from_millis(env_u64(
                "GATEWAY_CONNECT_TIMEOUT_MS",
                DEFAULT_CONNECT_TIMEOUT_MS,
            )),
            write_timeout: Duration::from_millis(env_u64(
                "GATEWAY_SEND_TIMEOUT_MS",
                DEFAULT_WRITE_TIMEOUT_MS,
            )),
            webrtc_connect_timeout: Duration::from_millis(env_u64(
                "WEBRTC_CONNECT_TIMEOUT_MS",
                DEFAULT_CONNECT_TIMEOUT_MS,
            )),
            done_timeout: Duration::from_millis(env_u64(
                "RUST_LIVE_SMOKE_DONE_TIMEOUT_MS",
                DEFAULT_DONE_TIMEOUT_MS,
            )),
            pre_audio_wait: Duration::from_millis(env_u64(
                "RUST_LIVE_SMOKE_PRE_AUDIO_WAIT_MS",
                DEFAULT_PRE_AUDIO_WAIT_MS,
            )),
            chunk_ms: env_u64("RUST_LIVE_SMOKE_CHUNK_MS", DEFAULT_CHUNK_MS).max(20),
            realtime_gap: env_bool("RUST_LIVE_SMOKE_REALTIME_GAP", false),
            downlink_wav_path: env_non_empty("RUST_LIVE_SMOKE_DOWNLINK_WAV_PATH")
                .map(PathBuf::from),
            rounds: env_u64("RUST_LIVE_SMOKE_ROUNDS", DEFAULT_ROUNDS),
            inter_round_gap: Duration::from_millis(env_u64_allow_zero(
                "RUST_LIVE_SMOKE_INTER_ROUND_GAP_MS",
                DEFAULT_INTER_ROUND_GAP_MS,
            )),
            quiet_audio_frames: env_bool("RUST_LIVE_SMOKE_QUIET_AUDIO_FRAMES", false),
            interrupt_after_playback: env_optional_u64(
                "RUST_LIVE_SMOKE_INTERRUPT_AFTER_PLAYBACK_MS",
            )
            .map(Duration::from_millis),
            natural_barge_in_after_playback: env_optional_u64(
                "RUST_LIVE_SMOKE_BARGE_IN_AFTER_PLAYBACK_MS",
            )
            .map(Duration::from_millis),
            interrupt_via_client_event: env_bool(
                "RUST_LIVE_SMOKE_INTERRUPT_VIA_CLIENT_EVENT",
                true,
            ),
            old_playback_report_delay: Duration::from_millis(env_u64_allow_zero(
                "RUST_LIVE_SMOKE_OLD_PLAYBACK_REPORT_DELAY_MS",
                0,
            )),
            strict_wake_interrupt: env_bool("RUST_LIVE_SMOKE_STRICT_WAKE_INTERRUPT", false),
            vision_fixture_path: env_non_empty("RUST_LIVE_SMOKE_VISION_FIXTURE").map(PathBuf::from),
            direct_text: env_non_empty("RUST_LIVE_SMOKE_DIRECT_TEXT"),
            vision_settle: Duration::from_millis(env_u64_allow_zero(
                "RUST_LIVE_SMOKE_VISION_SETTLE_MS",
                1200,
            )),
            post_rounds_hold: Duration::from_millis(env_u64_allow_zero(
                "RUST_LIVE_SMOKE_POST_ROUNDS_HOLD_MS",
                0,
            )),
        })
    }
}

fn inject_vision_fixture(cfg: &SmokeConfig) -> Result<()> {
    let Some(fixture_path) = cfg.vision_fixture_path.as_deref() else {
        return Ok(());
    };
    let snapshot_path = PathBuf::from(cfg_vision_snapshot_path());
    let image = std::fs::read(fixture_path)
        .with_context(|| format!("read vision fixture failed: {}", fixture_path.display()))?;
    if image.is_empty() {
        bail!("vision fixture is empty: {}", fixture_path.display());
    }
    std::fs::write(&snapshot_path, image)
        .with_context(|| format!("write vision snapshot failed: {}", snapshot_path.display()))?;
    println!(
        "vision_fixture_injected={} snapshot_path={}",
        fixture_path.display(),
        snapshot_path.display()
    );
    Ok(())
}

fn cfg_vision_snapshot_path() -> String {
    env_or("VISION_SNAPSHOT_PATH", "temp/pic.jpeg")
}

fn round_downlink_path(base: Option<&Path>, round: u64, rounds: u64) -> Option<PathBuf> {
    let base = base?;
    if rounds == 1 {
        return Some(base.to_path_buf());
    }
    let stem = base.file_stem()?.to_string_lossy();
    let extension = base.extension().map(|value| value.to_string_lossy());
    let file_name = match extension {
        Some(extension) => format!("{stem}.round-{round:03}.{extension}"),
        None => format!("{stem}.round-{round:03}"),
    };
    Some(base.with_file_name(file_name))
}

fn server_message_type(message: &ServerMessage) -> &'static str {
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

fn env_or(name: &str, fallback: &str) -> String {
    env_non_empty(name).unwrap_or_else(|| fallback.to_string())
}

fn env_non_empty(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn env_bool(name: &str, fallback: bool) -> bool {
    match env_non_empty(name).map(|value| value.to_ascii_lowercase()) {
        Some(value) if matches!(value.as_str(), "1" | "true" | "yes" | "on") => true,
        Some(value) if matches!(value.as_str(), "0" | "false" | "no" | "off") => false,
        Some(_) | None => fallback,
    }
}

fn env_u64(name: &str, fallback: u64) -> u64 {
    env_non_empty(name)
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(fallback)
}

fn env_u64_allow_zero(name: &str, fallback: u64) -> u64 {
    env_non_empty(name)
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(fallback)
}

fn env_optional_u64(name: &str) -> Option<u64> {
    env_non_empty(name).and_then(|value| value.parse::<u64>().ok())
}

fn unix_nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default()
}
