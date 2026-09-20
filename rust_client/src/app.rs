use crate::{
    audio::{
        capture::{start_capture, CaptureEvent, CaptureHandle},
        opus_codec::{
            configured_opus_bitrate_bps, encode_pcm16_opus_packet_stream, OpusStreamDecoder,
        },
        playback::{
            start_playback, PlaybackCommand, PlaybackEvent, PlaybackGeneration, PlaybackHandle,
            PlaybackStats,
        },
        wav::load_wav_for_playback,
    },
    audio_control::{spawn_audio_control_server, AudioDuckingState},
    audio_frontend::capture_tcp::start_capture_tcp,
    audio_frontend::control_tcp::{AudioFrontendControl, AudioFrontendStatus},
    audio_frontend::playback_tcp::start_playback_tcp,
    barge_in::{BargeInIdentity, BargeInProbeSchedule},
    config::{AudioFrontendMode, Config},
    environment_trigger::{
        spawn_environment_trigger_server, EnvironmentTalkResponse, EnvironmentTriggerQueue,
    },
    protocol::{AudioFrame, AudioFrameHeader, ClientMessage, ServerMessage},
    state::DialogueState,
    transport::{
        hybrid::{HybridConnectOptions, HybridTransport},
        TransportEvent, VoiceTransport, WebRtcAudioUplinkMode,
    },
    tts::processor::{
        spawn_tts_processor, TtsProcessorCommand, TtsProcessorEvent, TtsProcessorHandle,
    },
    vad::{detector::VadDetector, trigger::VadTriggerWindow},
    wake::{
        http_trigger::HttpWakeMonitor,
        kws_tcp_monitor::KwsWakeMonitor,
        serial_monitor::{SerialWakeEvent, SerialWakeMonitor},
    },
};
use anyhow::{Context, Result};
use crossbeam::channel::{Sender as CrossbeamSender, TryRecvError};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use std::{collections::VecDeque, path::Path};
use tokio::{
    sync::{
        mpsc::{self, error::TryRecvError as TokioTryRecvError},
        watch,
    },
    time,
};
use tracing::{error, info, warn};

const MAX_STALE_PLAYBACK_ROUNDS: usize = 16;
const MAX_STALE_SERVER_TRACES: usize = 32;
const PRE_PLAYBACK_RTP_TTL: Duration = Duration::from_millis(500);
const PRE_PLAYBACK_RTP_MAX_FRAMES: usize = 32;
const AUDIO_FRONTEND_CONTROL_RETRY_INTERVAL: Duration = Duration::from_secs(1);
const AUDIO_FRONTEND_CONTROL_HEALTH_INTERVAL: Duration = Duration::from_secs(1);

pub struct App {
    config: Config,
    audio_ducking: AudioDuckingState,
}

#[derive(Debug, Clone)]
struct PlaybackRound {
    round_id: String,
    playback_id: String,
    generation: u64,
    rtp_start_sequence: Option<u16>,
    first_audio_received_at: Option<Instant>,
    playback_started_at: Option<Instant>,
}

#[derive(Debug)]
struct PendingPrePlaybackRtp {
    received_at: Instant,
    generation: u64,
    frame: AudioFrame,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PlaybackEventDisposition {
    Current,
    Cancelled,
    Stale,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct PlaybackRoundKey {
    round_id: String,
    playback_id: String,
}

impl From<&PlaybackRound> for PlaybackRoundKey {
    fn from(round: &PlaybackRound) -> Self {
        Self {
            round_id: round.round_id.clone(),
            playback_id: round.playback_id.clone(),
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct StalePlaybackFilter<'a> {
    rounds: &'a [PlaybackRoundKey],
    trace_ids: &'a [String],
}

#[derive(Debug, Clone)]
struct RecordingUpload {
    utterance_id: String,
    trace_id: String,
    chunk_seq: u64,
    total_samples: usize,
    total_chunks: u64,
    total_packets: u64,
    total_opus_bytes: u64,
    started_at: Instant,
    chunk_samples: usize,
}

#[derive(Debug, Clone)]
struct PendingTurnCommit {
    trace_id: Option<String>,
    utterance_id: String,
    candidate_seq: u64,
    speech_epoch: u64,
    audio_watermark: u64,
    asr_text: Option<String>,
    asr_time_ms: Option<f64>,
    received_at: Instant,
}

impl RecordingUpload {
    fn has_audio(&self) -> bool {
        self.total_samples > 0
    }
}

fn current_epoch_ms() -> i128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as i128)
        .unwrap_or(0)
}

fn duration_ms_between(start: Option<Instant>, end: Option<Instant>) -> Option<f64> {
    Some(end?.duration_since(start?).as_secs_f64() * 1000.0)
}

impl App {
    pub fn new(config: Config) -> Self {
        let audio_ducking = AudioDuckingState::new(
            config.audio_control.default_ttl_ms,
            config.audio_control.max_ttl_ms,
        );
        Self {
            config,
            audio_ducking,
        }
    }

    fn recording_silence_limit(
        active_enabled: bool,
        vad_silence_secs: f32,
        failure_fallback_ms: u64,
        ambiguous_hard_timeout_ms: u64,
        continue_confirmed: bool,
    ) -> Duration {
        if !active_enabled {
            return Duration::from_secs_f32(vad_silence_secs);
        }
        Duration::from_millis(if continue_confirmed {
            ambiguous_hard_timeout_ms
        } else {
            failure_fallback_ms
        })
    }

    fn turn_commit_wait_ms(silence_elapsed_ms: u64, min_commit_silence_ms: u64) -> u64 {
        min_commit_silence_ms.saturating_sub(silence_elapsed_ms)
    }

    fn pending_turn_commit_matches(
        pending: &PendingTurnCommit,
        upload_utterance_id: &str,
        upload_total_samples: u64,
        candidate_seq: u64,
        speech_epoch: u64,
        candidate_sent_for_silence: bool,
    ) -> bool {
        pending.utterance_id == upload_utterance_id
            && pending.candidate_seq == candidate_seq
            && pending.speech_epoch == speech_epoch
            && pending.audio_watermark <= upload_total_samples
            && candidate_sent_for_silence
    }

    pub async fn run_forever(&mut self, mut shutdown: watch::Receiver<bool>) -> Result<()> {
        let _audio_control_task = spawn_audio_control_server(
            self.config.audio_control.clone(),
            self.audio_ducking.clone(),
        );
        let (_environment_trigger_task, environment_trigger_queue) =
            spawn_environment_trigger_server(self.config.environment_trigger.clone());
        let wake_monitor = if self.config.wake.source_mode.hardware_enabled() {
            Some(SerialWakeMonitor::new(self.config.wake.clone()))
        } else {
            None
        };
        let http_wake_monitor = if self.config.wake.source_mode.hardware_enabled() {
            Some(HttpWakeMonitor::spawn(self.config.wake.clone()))
        } else {
            None
        };
        let kws_wake_monitor = if self.config.wake.source_mode.kws_enabled() {
            Some(KwsWakeMonitor::spawn(self.config.wake.clone()))
        } else {
            None
        };
        let mut reconnect_delay_secs = 1u64;

        loop {
            if *shutdown.borrow() {
                info!("[SHUTDOWN] RustClient 已停止");
                return Ok(());
            }
            self.log_config();
            info!("[GATEWAY] 等待连接 Gateway...");

            match self
                .run_connected_session(
                    wake_monitor.as_ref(),
                    http_wake_monitor.as_ref(),
                    kws_wake_monitor.as_ref(),
                    environment_trigger_queue.clone(),
                    shutdown.clone(),
                )
                .await
            {
                Ok(()) => {
                    reconnect_delay_secs = 1;
                    warn!("[GATEWAY][WARN] 连接已关闭，准备重连");
                }
                Err(err) => {
                    error!(error = %err, "[GATEWAY][ERROR] 会话失败");
                }
            }

            if *shutdown.borrow() {
                info!("[SHUTDOWN] RustClient 已停止");
                return Ok(());
            }
            warn!("[GATEWAY][WARN] {} 秒后重连...", reconnect_delay_secs);
            tokio::select! {
                _ = time::sleep(Duration::from_secs(reconnect_delay_secs)) => {}
                changed = shutdown.changed() => {
                    if changed.is_ok() && *shutdown.borrow() {
                        info!("[SHUTDOWN] 重连等待已取消");
                        return Ok(());
                    }
                }
            }
            reconnect_delay_secs = (reconnect_delay_secs + 1).min(3);
        }
    }

    async fn run_connected_session(
        &mut self,
        wake_monitor: Option<&SerialWakeMonitor>,
        http_wake_monitor: Option<&HttpWakeMonitor>,
        kws_wake_monitor: Option<&KwsWakeMonitor>,
        environment_trigger_queue: EnvironmentTriggerQueue,
        mut shutdown: watch::Receiver<bool>,
    ) -> Result<()> {
        let session_started_at = Instant::now();
        let connect = HybridTransport::connect(HybridConnectOptions {
            url: &self.config.gateway.url,
            connect_timeout: Duration::from_secs_f32(self.config.gateway.connect_timeout_secs),
            write_timeout: Duration::from_secs_f32(self.config.gateway.send_timeout_secs),
            policy: self.config.transport.policy,
            webrtc_enabled: self.config.transport.webrtc_enabled,
            webrtc_connect_timeout: Duration::from_millis(
                self.config.transport.webrtc_connect_timeout_ms,
            ),
            fallback_enabled: self.config.transport.fallback_enabled,
            webrtc_audio_uplink_mode: self.config.transport.webrtc_audio_uplink_mode,
            vision: &self.config.vision,
        });
        tokio::pin!(connect);
        let (conn, server_events) = tokio::select! {
            result = &mut connect => result?,
            changed = shutdown.changed() => {
                if changed.is_ok() && *shutdown.borrow() {
                    info!("[SHUTDOWN] Gateway 连接过程已取消");
                    return Ok(());
                }
                return Err(anyhow::anyhow!("退出信号通道已关闭"));
            }
        };
        if let Some(robot_id) = self.config.gateway.robot_id.clone() {
            conn.send_control(&ClientMessage::Register {
                robot_id,
                robot_secret: self.config.gateway.robot_secret.clone(),
                client_type: "rust".to_string(),
            })
            .await?;
        }
        let mut capture: Option<CaptureHandle> =
            if self.config.audio.should_pause_capture_while_waiting() {
                info!("[AUDIO] 待机省电模式已开启，等待唤醒时暂停麦克风采集");
                None
            } else {
                Some(self.start_capture_for_dialogue("初始化")?)
            };
        let playback = self.start_playback_for_dialogue()?;
        let mut audio_frontend_control = self.connect_audio_frontend_control(false);
        let mut audio_frontend_speaking = false;
        let mut next_audio_frontend_control_retry_at =
            Instant::now() + AUDIO_FRONTEND_CONTROL_RETRY_INTERVAL;
        let mut next_audio_frontend_control_health_at =
            Instant::now() + AUDIO_FRONTEND_CONTROL_HEALTH_INTERVAL;

        let mut vad = VadDetector::new(self.config.vad.mode, self.config.audio.sample_rate)?;
        let trigger_frames = ((self.config.vad.speech_threshold_secs * 1000.0)
            / self.config.audio.frame_duration_ms as f32)
            .round()
            .max(1.0) as usize;
        let mut trigger_window =
            VadTriggerWindow::new(trigger_frames, self.config.vad.speech_trigger_ratio)?;

        let prebuffer_target =
            (self.config.tts.prebuffer_secs * self.config.audio.sample_rate as f32) as usize;
        let (tts_cmd_tx, mut tts_event_rx) = spawn_tts_processor(
            self.config.audio.sample_rate,
            self.config.audio.tts_source_rate,
            prebuffer_target,
            playback.command_tx.clone(),
            playback.generation.clone(),
        );
        let mut tts_opus_decoder = OpusStreamDecoder::new(self.config.audio.tts_source_rate, 1, 20)
            .context("初始化 TTS Opus 解码器失败")?;
        let tts_accepting_audio = Arc::new(AtomicBool::new(false));
        let (control_tx, mut control_rx) = mpsc::channel::<TransportEvent>(256);
        self.spawn_gateway_dispatch(server_events, control_tx);

        let mut pending_exit: Option<bool> = None;
        let asr_ding = self.load_asr_ding();
        let mut pending_playback_state: Option<DialogueState> = None;
        let mut state = DialogueState::WaitingForWakeWord;

        info!(
            policy = conn.policy().as_str(),
            transport = conn.kind().as_str(),
            "[GATEWAY] transport 已连接"
        );
        info!("[WAKE] 等待唤醒...");

        let mut last_heartbeat = Instant::now();
        let wake_debounce = Duration::from_secs_f32(self.config.wake.debounce_secs);
        let mut last_wake_accepted_at: Option<Instant> = None;
        let mut recording_frames: Vec<Vec<i16>> = Vec::new();
        let mut recording_upload: Option<RecordingUpload> = None;
        let mut recording_upload_seq: u64 = 0;
        let mut silence_started_at: Option<Instant> = None;
        let mut turn_candidate_seq: u64 = 0;
        let mut turn_candidate_speech_epoch: u64 = 0;
        let mut turn_candidate_sent_for_silence = false;
        let mut turn_candidate_continue_confirmed = false;
        let mut pending_turn_commit: Option<PendingTurnCommit> = None;
        let mut barge_in_upload: Option<RecordingUpload> = None;
        let mut barge_in_frames: Vec<Vec<i16>> = Vec::new();
        let mut barge_in_schedule: Option<BargeInProbeSchedule> = None;
        let mut barge_in_speech_epoch: u64 = 0;
        let mut barge_in_last_ignored_seq: u64 = 0;
        let mut barge_in_silence_started_at: Option<Instant> = None;
        let mut next_capture_restart_at: Option<Instant> = None;
        let mut suppress_stale_server_messages = false;
        let mut playback_deadline: Option<(Instant, &'static str)> = None;
        let mut current_playback_round: Option<PlaybackRound> = None;
        let mut pending_pre_playback_rtp: VecDeque<PendingPrePlaybackRtp> = VecDeque::new();
        let mut stale_playback_rounds: Vec<PlaybackRoundKey> = Vec::new();
        let mut stale_server_trace_ids: Vec<String> = Vec::new();
        let mut pending_response_trace_id: Option<String> = None;
        let mut pending_playback_interrupt_reason: Option<String> = None;
        let mut last_environment_talk_at: Option<Instant> = None;
        let mut standby_notification_sent = true;
        let mut session_ready = false;
        let wake_capture_prebuffer_capacity = if self.config.audio.wake_capture_prebuffer_ms == 0 {
            0
        } else {
            self.config
                .audio
                .wake_capture_prebuffer_ms
                .div_ceil(self.config.audio.frame_duration_ms) as usize
        };
        let barge_in_preroll_capacity =
            400u32.div_ceil(self.config.audio.frame_duration_ms).max(1) as usize;
        let mut wake_capture_prebuffer: VecDeque<Vec<i16>> =
            VecDeque::with_capacity(wake_capture_prebuffer_capacity);
        let mut collect_wake_capture_prebuffer = false;
        let mut recording_preroll: VecDeque<Vec<i16>> =
            VecDeque::with_capacity(wake_capture_prebuffer_capacity);

        loop {
            if *shutdown.borrow() {
                info!("[SHUTDOWN] 正在清理当前语音会话");
                Self::cancel_tts_generation(
                    &playback.generation,
                    &playback.command_tx,
                    &tts_cmd_tx,
                    "process_shutdown",
                );
                Self::set_audio_frontend_speaking(
                    &mut audio_frontend_control,
                    &mut audio_frontend_speaking,
                    self.config.audio.frontend_mute_mic_during_playback,
                    false,
                    "process_shutdown",
                );
                tts_accepting_audio.store(false, Ordering::Relaxed);
                let cancel_recording = self.cancel_recording_audio_stream(
                    &conn,
                    &mut recording_upload,
                    &mut recording_frames,
                    "process_shutdown",
                );
                if time::timeout(Duration::from_millis(750), cancel_recording)
                    .await
                    .is_err()
                {
                    recording_upload.take();
                    recording_frames.clear();
                    warn!("[SHUTDOWN] 录音取消通知超时，继续退出");
                }
                match time::timeout(
                    Duration::from_millis(750),
                    conn.send_control(&ClientMessage::Interrupt { reason: None }),
                )
                .await
                {
                    Ok(Ok(())) => {}
                    Ok(Err(err)) => {
                        warn!(error = %err, "[SHUTDOWN] Gateway interrupt 发送失败");
                    }
                    Err(_) => {
                        warn!("[SHUTDOWN] Gateway interrupt 发送超时");
                    }
                }
                info!("[SHUTDOWN] 当前语音会话清理完成");
                return Ok(());
            }
            let control_now = Instant::now();
            if control_now >= next_audio_frontend_control_health_at {
                let control_failed = audio_frontend_control
                    .as_mut()
                    .and_then(|control| control.ping().err());
                if let Some(err) = control_failed {
                    warn!(
                        "[AUDIO_FRONTEND][WARN] control TCP 健康检查失败，准备重连: {}",
                        err
                    );
                    audio_frontend_control = None;
                    next_audio_frontend_control_retry_at = control_now;
                }
                next_audio_frontend_control_health_at =
                    control_now + AUDIO_FRONTEND_CONTROL_HEALTH_INTERVAL;
            }
            if self.config.audio.frontend_mode == AudioFrontendMode::Tcp
                && audio_frontend_control.is_none()
                && Instant::now() >= next_audio_frontend_control_retry_at
            {
                audio_frontend_control =
                    self.connect_audio_frontend_control(audio_frontend_speaking);
                next_audio_frontend_control_retry_at =
                    Instant::now() + AUDIO_FRONTEND_CONTROL_RETRY_INTERVAL;
            }

            if let Some(capture_event) = capture
                .as_ref()
                .map(|active_capture| active_capture.events.try_recv())
            {
                match capture_event {
                    Ok(CaptureEvent::StreamError(err)) => {
                        warn!("[AUDIO][WARN] 采集流异常: {}", err);
                        let interrupted_recording =
                            matches!(state, DialogueState::Recording { .. });
                        let now = Instant::now();
                        let ready = next_capture_restart_at.map(|t| now >= t).unwrap_or(true);
                        if ready {
                            self.cancel_recording_audio_stream(
                                &conn,
                                &mut recording_upload,
                                &mut recording_frames,
                                "capture_restart",
                            )
                            .await;
                            Self::reset_recording_state(
                                &mut trigger_window,
                                &mut recording_frames,
                                &mut silence_started_at,
                            );
                            Self::clear_wake_audio_buffers(
                                &mut wake_capture_prebuffer,
                                &mut recording_preroll,
                                &mut collect_wake_capture_prebuffer,
                            );
                            self.drain_capture_frames(capture.as_ref());
                            if interrupted_recording {
                                let _ = conn
                                    .send_control(&ClientMessage::Interrupt { reason: None })
                                    .await;
                                state = Self::awake_idle_state();
                            }
                            if self.config.audio.frontend_mode == AudioFrontendMode::Tcp {
                                next_capture_restart_at =
                                    Some(now + AUDIO_FRONTEND_CONTROL_RETRY_INTERVAL);
                                continue;
                            }
                            match self.start_capture_for_dialogue("重启") {
                                Ok(new_capture) => {
                                    capture = Some(new_capture);
                                    next_capture_restart_at = Some(now + Duration::from_secs(1));
                                }
                                Err(e) => {
                                    warn!("[AUDIO][WARN] 采集重启失败: {}，触发重连", e);
                                    return Err(anyhow::anyhow!("采集重启失败: {e}"));
                                }
                            }
                        } else {
                            warn!("[AUDIO][WARN] 采集重启冷却中，跳过");
                        }
                    }
                    Err(TryRecvError::Disconnected) => {
                        return Err(anyhow::anyhow!("采集事件通道已断开"));
                    }
                    Err(TryRecvError::Empty) => {}
                }
            }

            match playback.event_rx.try_recv() {
                Ok(PlaybackEvent::Started { generation })
                    if Self::classify_playback_event(
                        current_playback_round.as_ref(),
                        generation,
                        playback.generation.current(),
                    ) == PlaybackEventDisposition::Current =>
                {
                    if let Some(playback_round) = current_playback_round.as_mut() {
                        if playback_round.playback_started_at.is_none() {
                            let started_at = Instant::now();
                            playback_round.playback_started_at = Some(started_at);
                            let first_audio_to_playback_ms = playback_round
                                .first_audio_received_at
                                .map(|first_audio_at| {
                                    started_at.duration_since(first_audio_at).as_secs_f64() * 1000.0
                                })
                                .unwrap_or(0.0);
                            info!(
                                round_id = %playback_round.round_id,
                                playback_id = %playback_round.playback_id,
                                first_audio_to_playback_ms,
                                "[TTFA] 播放器开始输出首个样本"
                            );
                        }
                    }
                }
                Ok(PlaybackEvent::Started { generation }) => {
                    tracing::debug!(generation, "[PLAY] 忽略过期 started 事件");
                }
                Ok(PlaybackEvent::Finished { generation, stats })
                    if Self::classify_playback_event(
                        current_playback_round.as_ref(),
                        generation,
                        playback.generation.current(),
                    ) == PlaybackEventDisposition::Current =>
                {
                    Self::set_audio_frontend_speaking(
                        &mut audio_frontend_control,
                        &mut audio_frontend_speaking,
                        self.config.audio.frontend_mute_mic_during_playback,
                        false,
                        "playback_finished",
                    );
                    playback_deadline = None;
                    info!(
                        "[PLAY] 播放完成 (chunks={}, samples={}, underruns={}, zero_fill_samples={}, max_buffered_samples={})",
                        stats.pushed_chunks,
                        stats.pushed_samples,
                        stats.underrun_callbacks,
                        stats.zero_filled_samples,
                        stats.max_buffered_samples,
                    );
                    if let Some(playback_round) = current_playback_round.take() {
                        pending_playback_interrupt_reason = None;
                        self.send_playback_report(&conn, playback_round, stats, None)
                            .await;
                    }
                    tts_accepting_audio.store(false, Ordering::Relaxed);
                    Self::reset_recording_state(
                        &mut trigger_window,
                        &mut recording_frames,
                        &mut silence_started_at,
                    );
                    Self::clear_wake_audio_buffers(
                        &mut wake_capture_prebuffer,
                        &mut recording_preroll,
                        &mut collect_wake_capture_prebuffer,
                    );
                    let exit = pending_exit.take().unwrap_or(false);
                    if let Some(next_state) = pending_playback_state.take() {
                        state = next_state;
                    } else if exit {
                        info!("[STATE] 退出对话，等待唤醒...");
                        self.cancel_recording_audio_stream(
                            &conn,
                            &mut recording_upload,
                            &mut recording_frames,
                            "exit",
                        )
                        .await;
                        Self::reset_recording_state(
                            &mut trigger_window,
                            &mut recording_frames,
                            &mut silence_started_at,
                        );
                        state = DialogueState::WaitingForWakeWord;
                    } else {
                        info!("[AUDIO] 请说话...");
                        state = Self::awake_idle_state();
                    }
                }
                Ok(PlaybackEvent::Finished { generation, stats })
                    if Self::classify_playback_event(
                        current_playback_round.as_ref(),
                        generation,
                        playback.generation.current(),
                    ) == PlaybackEventDisposition::Cancelled =>
                {
                    Self::set_audio_frontend_speaking(
                        &mut audio_frontend_control,
                        &mut audio_frontend_speaking,
                        self.config.audio.frontend_mute_mic_during_playback,
                        false,
                        "playback_cancelled_after_finish",
                    );
                    playback_deadline = None;
                    info!(
                        generation,
                        "[PLAY] 已取消轮次的完成事件按打断终态处理 (chunks={}, samples={})",
                        stats.pushed_chunks,
                        stats.pushed_samples,
                    );
                    if let Some(playback_round) = current_playback_round.take() {
                        let reason = pending_playback_interrupt_reason.take();
                        self.send_playback_report(&conn, playback_round, stats, reason)
                            .await;
                    }
                }
                Ok(PlaybackEvent::Finished { generation, .. }) => {
                    tracing::debug!(generation, "[PLAY] 忽略过期 finished 事件");
                }
                Ok(PlaybackEvent::Interrupted { generation, stats })
                    if Self::classify_playback_event(
                        current_playback_round.as_ref(),
                        generation,
                        playback.generation.current(),
                    ) != PlaybackEventDisposition::Stale =>
                {
                    Self::set_audio_frontend_speaking(
                        &mut audio_frontend_control,
                        &mut audio_frontend_speaking,
                        self.config.audio.frontend_mute_mic_during_playback,
                        false,
                        "playback_interrupted",
                    );
                    info!(
                        "[PLAY] 播放已打断 (chunks={}, samples={})",
                        stats.pushed_chunks, stats.pushed_samples,
                    );
                    if let Some(playback_round) = current_playback_round.take() {
                        let reason = pending_playback_interrupt_reason.take();
                        self.send_playback_report(&conn, playback_round, stats, reason)
                            .await;
                    }
                }
                Ok(PlaybackEvent::Interrupted { generation, .. }) => {
                    tracing::debug!(generation, "[PLAY] 忽略过期 interrupted 事件");
                }
                Ok(PlaybackEvent::StreamError(err)) => {
                    Self::set_audio_frontend_speaking(
                        &mut audio_frontend_control,
                        &mut audio_frontend_speaking,
                        self.config.audio.frontend_mute_mic_during_playback,
                        false,
                        "playback_stream_error",
                    );
                    playback_deadline = None;
                    warn!("[PLAY][WARN] 播放流错误: {}", err);

                    if self.config.audio.frontend_mode == AudioFrontendMode::Cpal {
                        return Err(anyhow::anyhow!("本地 CPAL 播放流失败，重建语音会话: {err}"));
                    }

                    if current_playback_round.is_some() {
                        Self::remember_stale_playback_round(
                            current_playback_round.as_ref(),
                            &mut stale_playback_rounds,
                            "playback_stream_error",
                        );
                        Self::cancel_tts_generation(
                            &playback.generation,
                            &playback.command_tx,
                            &tts_cmd_tx,
                            "playback_stream_error",
                        );
                        tts_accepting_audio.store(false, Ordering::Relaxed);
                        Self::reset_tts_opus_decoder(
                            &mut tts_opus_decoder,
                            "playback_stream_error",
                        );
                        if let Some(playback_round) = current_playback_round.take() {
                            self.send_playback_report(
                                &conn,
                                playback_round,
                                PlaybackStats::default(),
                                Some("playback_stream_error".to_string()),
                            )
                            .await;
                        }
                        pending_exit = None;
                        state = pending_playback_state
                            .take()
                            .unwrap_or_else(|| match state {
                                DialogueState::WaitingForWakeWord => {
                                    DialogueState::WaitingForWakeWord
                                }
                                _ => Self::awake_idle_state(),
                            });
                        info!("[PLAY] 本地播放流异常轮次已终止，等待播放链路恢复");
                    }
                }
                Err(TryRecvError::Disconnected) => {
                    return Err(anyhow::anyhow!("播放事件通道已断开"));
                }
                Err(TryRecvError::Empty) => {}
            }

            loop {
                match tts_event_rx.try_recv() {
                    Ok(TtsProcessorEvent::PlaybackStarted { generation })
                        if playback.generation.is_current(generation) =>
                    {
                        if state.can_start_tts_playback() {
                            if Self::should_clear_wake_prebuffer_on_playback_started(
                                self.config.audio.frontend_mode,
                                self.config.audio.frontend_mute_mic_during_playback,
                                collect_wake_capture_prebuffer,
                            ) {
                                wake_capture_prebuffer.clear();
                                collect_wake_capture_prebuffer = false;
                            }
                            trigger_window.clear();
                            recording_preroll.clear();
                            silence_started_at = None;
                            barge_in_speech_epoch = 0;
                            barge_in_silence_started_at = None;
                            Self::set_audio_frontend_speaking(
                                &mut audio_frontend_control,
                                &mut audio_frontend_speaking,
                                self.config.audio.frontend_mute_mic_during_playback,
                                true,
                                "tts_playback_started",
                            );
                            info!("[TTS] 开始播放...");
                            playback_deadline = Some((
                                Instant::now()
                                    + Duration::from_secs(
                                        self.config.runtime.tts_playback_timeout_secs,
                                    ),
                                "TTS播放",
                            ));
                            state = DialogueState::TtsPlaying;
                        }
                    }
                    Ok(TtsProcessorEvent::PlaybackStarted { .. }) => {}
                    Err(TokioTryRecvError::Empty) => break,
                    Err(TokioTryRecvError::Disconnected) => {
                        return Err(anyhow::anyhow!("TTS 处理任务已断开"));
                    }
                }
            }

            while let Some(event) =
                Self::try_recv_wake_event(wake_monitor, http_wake_monitor, kws_wake_monitor)
            {
                if event.received_at < session_started_at {
                    continue;
                }

                let details = self.format_wake_event(&event);
                if !Self::accept_wake_event(&event, wake_debounce, &mut last_wake_accepted_at) {
                    info!("[WAKE] 忽略重复唤醒: {}", details);
                    continue;
                }

                let wake_event = Self::wake_event_for_state(&state);
                info!(
                    event = wake_event,
                    state = ?state,
                    "[WAKE] 唤醒: {}",
                    details
                );
                Self::cancel_tts_generation(
                    &playback.generation,
                    &playback.command_tx,
                    &tts_cmd_tx,
                    "wake_interrupt",
                );
                Self::set_audio_frontend_speaking(
                    &mut audio_frontend_control,
                    &mut audio_frontend_speaking,
                    self.config.audio.frontend_mute_mic_during_playback,
                    false,
                    "wake_interrupt",
                );
                tts_accepting_audio.store(false, Ordering::Relaxed);
                Self::reset_tts_opus_decoder(&mut tts_opus_decoder, "wake_reset");
                pending_exit = None;
                pending_playback_state = None;
                pending_playback_interrupt_reason = if wake_event == "wake_interrupt" {
                    Some("wake_interrupt".to_string())
                } else {
                    None
                };
                if wake_event == "wake_interrupt" {
                    Self::remember_stale_playback_round(
                        current_playback_round.as_ref(),
                        &mut stale_playback_rounds,
                        "wake_interrupt",
                    );
                    Self::remember_stale_server_trace(
                        pending_response_trace_id.take(),
                        &mut stale_server_trace_ids,
                        "wake_interrupt",
                    );
                }
                playback_deadline = None;
                suppress_stale_server_messages =
                    Self::should_suppress_stale_server_messages_on_wake(wake_event);
                self.cancel_recording_audio_stream(
                    &conn,
                    &mut recording_upload,
                    &mut recording_frames,
                    "wake_interrupt",
                )
                .await;
                Self::reset_recording_state(
                    &mut trigger_window,
                    &mut recording_frames,
                    &mut silence_started_at,
                );
                self.ensure_capture_running(&mut capture, "唤醒")?;
                if Self::should_drain_capture_after_wake(self.config.audio.frontend_mode) {
                    self.drain_capture_frames(capture.as_ref());
                }
                Self::clear_wake_audio_buffers(
                    &mut wake_capture_prebuffer,
                    &mut recording_preroll,
                    &mut collect_wake_capture_prebuffer,
                );
                collect_wake_capture_prebuffer = wake_capture_prebuffer_capacity > 0;
                let event_id = self
                    .send_client_event(&conn, wake_event, "hardware_wake")
                    .await
                    .context("发送云端唤醒播报失败")?;
                pending_response_trace_id = Some(event_id);
                info!("[WAKE] 已请求云端唤醒播报: {}", wake_event);
                state = DialogueState::SendingAudio {
                    last_interaction: Instant::now(),
                    waiting_started_at: Instant::now(),
                };
            }

            while let Some(request) = environment_trigger_queue.take_latest() {
                if request.is_closed() {
                    continue;
                }

                if request.received_at < session_started_at {
                    request.respond(EnvironmentTalkResponse::ignored("gateway_disconnected"));
                    continue;
                }

                if !session_ready {
                    info!("[ENV] 环境触发（忽略，会话尚未注册完成）");
                    request.respond(EnvironmentTalkResponse::ignored("session_not_ready"));
                    continue;
                }

                if !matches!(state, DialogueState::WaitingForWakeWord)
                    || pending_playback_state.is_some()
                    || playback_deadline.is_some()
                {
                    info!("[ENV] 环境触发（忽略，非待机状态）");
                    request.respond(EnvironmentTalkResponse::ignored("not_waiting"));
                    continue;
                }

                if last_environment_talk_at
                    .map(|last| {
                        last.elapsed()
                            < Duration::from_secs_f32(self.config.environment_trigger.debounce_secs)
                    })
                    .unwrap_or(false)
                {
                    info!("[ENV] 环境触发（忽略，防抖中）");
                    request.respond(EnvironmentTalkResponse::ignored("debounced"));
                    continue;
                }

                let content = request.content.clone();
                info!(
                    "[ENV] 环境触发输入: content_chars={}",
                    content.chars().count()
                );
                if let Err(err) = self.ensure_capture_running(&mut capture, "环境触发") {
                    request.respond(EnvironmentTalkResponse::ignored("capture_unavailable"));
                    return Err(err).context("环境触发启动麦克风采集失败");
                }
                self.drain_capture_frames(capture.as_ref());
                self.cancel_recording_audio_stream(
                    &conn,
                    &mut recording_upload,
                    &mut recording_frames,
                    "environment_trigger",
                )
                .await;
                Self::reset_recording_state(
                    &mut trigger_window,
                    &mut recording_frames,
                    &mut silence_started_at,
                );
                Self::clear_wake_audio_buffers(
                    &mut wake_capture_prebuffer,
                    &mut recording_preroll,
                    &mut collect_wake_capture_prebuffer,
                );
                pending_exit = None;
                pending_playback_state = None;
                pending_playback_interrupt_reason = None;
                playback_deadline = None;
                suppress_stale_server_messages = false;
                if let Err(err) = self.send_environment_text(&conn, &content).await {
                    request.respond(EnvironmentTalkResponse::ignored("gateway_send_failed"));
                    return Err(err).context("环境触发文本发送 Gateway 失败");
                }
                last_environment_talk_at = Some(Instant::now());
                standby_notification_sent = false;
                state = DialogueState::SendingAudio {
                    last_interaction: Instant::now(),
                    waiting_started_at: Instant::now(),
                };
                request.respond(EnvironmentTalkResponse::accepted());
            }

            if let Some(capture_frame) = capture
                .as_ref()
                .map(|active_capture| active_capture.frames.try_recv())
            {
                match capture_frame {
                    Ok(frame) => match &mut state {
                        DialogueState::WaitingForWakeWord => {
                            trigger_window.clear();
                            Self::clear_wake_audio_buffers(
                                &mut wake_capture_prebuffer,
                                &mut recording_preroll,
                                &mut collect_wake_capture_prebuffer,
                            );
                        }
                        DialogueState::AwakeIdle { last_interaction } => {
                            if last_interaction.elapsed()
                                >= Duration::from_secs(self.config.runtime.session_timeout_secs)
                            {
                                trigger_window.clear();
                                self.pause_capture_for_waiting(&mut capture);
                                pending_playback_state = Some(DialogueState::WaitingForWakeWord);
                                Self::clear_wake_audio_buffers(
                                    &mut wake_capture_prebuffer,
                                    &mut recording_preroll,
                                    &mut collect_wake_capture_prebuffer,
                                );
                                let event_id = self
                                    .send_client_event(&conn, "sleep_exit", "session_timeout")
                                    .await
                                    .context("发送云端待机播报失败")?;
                                pending_response_trace_id = Some(event_id);
                                playback_deadline = None;
                                state = DialogueState::SendingAudio {
                                    last_interaction: *last_interaction,
                                    waiting_started_at: Instant::now(),
                                };
                                info!("[STATE] 会话超时，等待唤醒...");
                                continue;
                            }

                            if collect_wake_capture_prebuffer {
                                let prebuffered_frames = wake_capture_prebuffer.len();
                                let speech_frames = Self::drain_wake_capture_prebuffer(
                                    &mut wake_capture_prebuffer,
                                    &mut trigger_window,
                                    &mut recording_preroll,
                                    wake_capture_prebuffer_capacity,
                                    &mut vad,
                                    self.config.vad.speech_start_min_dbfs,
                                );
                                collect_wake_capture_prebuffer = false;
                                if prebuffered_frames > 0 {
                                    info!(
                                        frames = prebuffered_frames,
                                        speech_frames, "[AUDIO] 已合入唤醒预缓冲音频"
                                    );
                                }
                            }

                            let is_trigger_speech = vad
                                .is_start_trigger_speech(
                                    &frame,
                                    self.config.vad.speech_start_min_dbfs,
                                )
                                .unwrap_or(false);
                            Self::push_audio_prebuffer(
                                &mut recording_preroll,
                                wake_capture_prebuffer_capacity,
                                frame.clone(),
                            );
                            trigger_window.push(frame, is_trigger_speech);
                            if trigger_window.should_trigger() {
                                recording_upload_seq = recording_upload_seq.wrapping_add(1);
                                recording_upload = Some(
                                    self.start_recording_audio_stream(&conn, recording_upload_seq)
                                        .await?,
                                );
                                recording_frames = if recording_preroll.is_empty() {
                                    trigger_window.drain_frames()
                                } else {
                                    trigger_window.clear();
                                    recording_preroll.drain(..).collect()
                                };
                                silence_started_at = None;
                                turn_candidate_seq = 0;
                                turn_candidate_speech_epoch = 0;
                                turn_candidate_sent_for_silence = false;
                                turn_candidate_continue_confirmed = false;
                                pending_turn_commit = None;
                                if let Some(upload) = recording_upload.as_mut() {
                                    self.flush_recording_audio_chunks(
                                        &conn,
                                        upload,
                                        &mut recording_frames,
                                        false,
                                    )
                                    .await?;
                                }
                                state = DialogueState::Recording {
                                    started_at: Instant::now(),
                                    last_interaction: *last_interaction,
                                };
                                info!("[REC] 检测到语音，开始录音...");
                            }
                        }
                        DialogueState::Recording {
                            started_at,
                            last_interaction,
                        } => {
                            let is_speech = vad
                                .is_start_trigger_speech(
                                    &frame,
                                    self.config.vad.speech_start_min_dbfs,
                                )
                                .unwrap_or(false);
                            recording_frames.push(frame);
                            if let Some(upload) = recording_upload.as_mut() {
                                self.flush_recording_audio_chunks(
                                    &conn,
                                    upload,
                                    &mut recording_frames,
                                    false,
                                )
                                .await?;
                            }

                            if is_speech {
                                if let Some(pending) = pending_turn_commit.take() {
                                    info!(
                                        utterance_id = %pending.utterance_id,
                                        candidate_seq = pending.candidate_seq,
                                        speech_epoch = pending.speech_epoch,
                                        "[TURN_GATE][ACTIVE] 稳定静音确认前恢复说话，取消待提交请求"
                                    );
                                }
                                if turn_candidate_sent_for_silence {
                                    turn_candidate_speech_epoch =
                                        turn_candidate_speech_epoch.wrapping_add(1);
                                    if self.config.turn_gate_shadow.active_enabled {
                                        if let Some(upload) = recording_upload.as_ref() {
                                            let cancel = ClientMessage::TurnCandidateCancel {
                                                bot_id: self.config.gateway.bot_id.clone(),
                                                trace_id: Some(upload.trace_id.clone()),
                                                utterance_id: upload.utterance_id.clone(),
                                                candidate_seq: turn_candidate_seq,
                                                speech_epoch: turn_candidate_speech_epoch,
                                                audio_watermark: upload.total_samples as u64,
                                                reason: "speech_resumed".to_string(),
                                            };
                                            if let Err(err) = conn.send_control(&cancel).await {
                                                warn!(
                                                    utterance_id = %upload.utterance_id,
                                                    candidate_seq = turn_candidate_seq,
                                                    speech_epoch = turn_candidate_speech_epoch,
                                                    error = %err,
                                                    "[TURN_GATE][ACTIVE] 恢复说话取消发送失败"
                                                );
                                            } else {
                                                info!(
                                                    utterance_id = %upload.utterance_id,
                                                    candidate_seq = turn_candidate_seq,
                                                    speech_epoch = turn_candidate_speech_epoch,
                                                    "[TURN_GATE][ACTIVE] 恢复说话，候选已取消"
                                                );
                                            }
                                        }
                                    }
                                }
                                silence_started_at = None;
                                turn_candidate_sent_for_silence = false;
                                turn_candidate_continue_confirmed = false;
                            } else if silence_started_at.is_none() {
                                silence_started_at = Some(Instant::now());
                            }

                            if self.config.turn_gate_shadow.enabled
                                && !turn_candidate_sent_for_silence
                                && silence_started_at
                                    .map(|started| {
                                        started.elapsed().as_millis() as u64
                                            >= self.config.turn_gate_shadow.candidate_silence_ms
                                    })
                                    .unwrap_or(false)
                            {
                                turn_candidate_sent_for_silence = true;
                                turn_candidate_continue_confirmed = false;
                                if let Some(upload) = recording_upload.as_ref() {
                                    turn_candidate_seq = turn_candidate_seq.wrapping_add(1);
                                    let silence_ms = silence_started_at
                                        .map(|started| started.elapsed().as_millis() as u64)
                                        .unwrap_or_default();
                                    let message = ClientMessage::TurnCandidate {
                                        bot_id: self.config.gateway.bot_id.clone(),
                                        trace_id: Some(upload.trace_id.clone()),
                                        utterance_id: upload.utterance_id.clone(),
                                        candidate_seq: turn_candidate_seq,
                                        speech_epoch: turn_candidate_speech_epoch,
                                        audio_watermark: upload.total_samples as u64,
                                        silence_ms,
                                        shadow: !self.config.turn_gate_shadow.active_enabled,
                                    };
                                    if let Err(err) = conn.send_control(&message).await {
                                        warn!(
                                            utterance_id = %upload.utterance_id,
                                            candidate_seq = turn_candidate_seq,
                                            error = %err,
                                            active = self.config.turn_gate_shadow.active_enabled,
                                            "[TURN_GATE] candidate 发送失败，主录音继续"
                                        );
                                    } else {
                                        info!(
                                            utterance_id = %upload.utterance_id,
                                            candidate_seq = turn_candidate_seq,
                                            speech_epoch = turn_candidate_speech_epoch,
                                            audio_watermark = upload.total_samples as u64,
                                            silence_ms,
                                            active = self.config.turn_gate_shadow.active_enabled,
                                            "[TURN_GATE] candidate 已发送，主录音保持 Recording"
                                        );
                                    }
                                }
                            }

                            let recording_too_long = started_at.elapsed()
                                >= Duration::from_secs(self.config.vad.max_recording_duration_secs);
                            let silence_limit = Self::recording_silence_limit(
                                self.config.turn_gate_shadow.active_enabled,
                                self.config.vad.silence_threshold_secs,
                                self.config.turn_gate_shadow.failure_fallback_ms,
                                self.config.turn_gate_shadow.ambiguous_hard_timeout_ms,
                                turn_candidate_continue_confirmed,
                            );
                            let silence_too_long = silence_started_at
                                .map(|s| s.elapsed() >= silence_limit)
                                .unwrap_or(false);

                            if recording_too_long || silence_too_long {
                                let Some(mut upload) = recording_upload.take() else {
                                    Self::reset_recording_state(
                                        &mut trigger_window,
                                        &mut recording_frames,
                                        &mut silence_started_at,
                                    );
                                    recording_preroll.clear();
                                    info!("[AUDIO] 请说话...");
                                    state = Self::awake_idle_state();
                                    continue;
                                };
                                let samples_len = self
                                    .finish_recording_audio_stream(
                                        &conn,
                                        &mut upload,
                                        &mut recording_frames,
                                    )
                                    .await?;
                                Self::schedule_feedback(
                                    playback.command_tx.clone(),
                                    playback.generation.clone(),
                                    asr_ding.as_deref(),
                                    Duration::from_millis(150),
                                    "语音提交确认音",
                                );
                                info!(
                                    "[REC] 已发送录音: chunks={} / {} 样本 / {:.2}s",
                                    upload.total_chunks,
                                    samples_len,
                                    samples_len as f32 / self.config.audio.sample_rate as f32,
                                );

                                recording_frames.clear();
                                recording_preroll.clear();
                                silence_started_at = None;
                                turn_candidate_seq = 0;
                                turn_candidate_speech_epoch = 0;
                                turn_candidate_sent_for_silence = false;
                                turn_candidate_continue_confirmed = false;
                                pending_turn_commit = None;
                                trigger_window.clear();
                                state = DialogueState::SendingAudio {
                                    last_interaction: *last_interaction,
                                    waiting_started_at: Instant::now(),
                                };
                            }
                        }
                        DialogueState::SendingAudio { .. } => {
                            if collect_wake_capture_prebuffer {
                                Self::push_audio_prebuffer(
                                    &mut wake_capture_prebuffer,
                                    wake_capture_prebuffer_capacity,
                                    frame,
                                );
                            }
                        }
                        DialogueState::TtsPlaying => {
                            if self.config.audio.should_detect_natural_barge_in() {
                                let is_trigger_speech = vad
                                    .is_start_trigger_speech(
                                        &frame,
                                        self.config.vad.speech_start_min_dbfs,
                                    )
                                    .unwrap_or(false);
                                Self::push_audio_prebuffer(
                                    &mut recording_preroll,
                                    barge_in_preroll_capacity,
                                    frame.clone(),
                                );

                                if barge_in_upload.is_none() {
                                    trigger_window.push(frame, is_trigger_speech);
                                    if trigger_window.should_trigger() && barge_in_speech_epoch < 3
                                    {
                                        if let Some(round) = current_playback_round.as_ref() {
                                            barge_in_speech_epoch += 1;
                                            recording_upload_seq =
                                                recording_upload_seq.wrapping_add(1);
                                            let upload = self
                                                .start_barge_in_audio_stream(
                                                    &conn,
                                                    recording_upload_seq,
                                                    round,
                                                    barge_in_speech_epoch,
                                                )
                                                .await?;
                                            let identity = BargeInIdentity {
                                                utterance_id: upload.utterance_id.clone(),
                                                round_id: round.round_id.clone(),
                                                playback_id: round.playback_id.clone(),
                                                speech_epoch: barge_in_speech_epoch,
                                            };
                                            barge_in_schedule = Some(BargeInProbeSchedule::new(
                                                identity,
                                                self.config.audio.sample_rate,
                                            ));
                                            barge_in_upload = Some(upload);
                                            barge_in_frames = recording_preroll.drain(..).collect();
                                            let preroll_samples =
                                                (self.config.audio.sample_rate as usize * 400
                                                    / 1_000)
                                                    * self.config.audio.channels as usize;
                                            let retained_samples: usize =
                                                barge_in_frames.iter().map(Vec::len).sum();
                                            if retained_samples > preroll_samples {
                                                let mut discard =
                                                    retained_samples - preroll_samples;
                                                while discard > 0 && !barge_in_frames.is_empty() {
                                                    if barge_in_frames[0].len() <= discard {
                                                        discard -= barge_in_frames.remove(0).len();
                                                    } else {
                                                        barge_in_frames[0].drain(..discard);
                                                        discard = 0;
                                                    }
                                                }
                                            }
                                            trigger_window.clear();
                                            barge_in_silence_started_at = None;
                                            barge_in_last_ignored_seq = 0;
                                        }
                                    }
                                } else {
                                    barge_in_frames.push(frame);
                                    if let Some(upload) = barge_in_upload.as_mut() {
                                        self.flush_recording_audio_chunks(
                                            &conn,
                                            upload,
                                            &mut barge_in_frames,
                                            false,
                                        )
                                        .await?;
                                    }

                                    if is_trigger_speech {
                                        barge_in_silence_started_at = None;
                                    } else if barge_in_silence_started_at.is_none() {
                                        barge_in_silence_started_at = Some(Instant::now());
                                    }

                                    if let (Some(upload), Some(schedule)) =
                                        (barge_in_upload.as_ref(), barge_in_schedule.as_mut())
                                    {
                                        let may_probe = schedule.current_candidate_seq() == 0
                                            || barge_in_last_ignored_seq
                                                == schedule.current_candidate_seq();
                                        if let Some(probe) = may_probe
                                            .then(|| {
                                                schedule.next_probe(upload.total_samples as u64)
                                            })
                                            .flatten()
                                        {
                                            let identity = schedule.identity();
                                            conn.send_control(&ClientMessage::BargeInProbe {
                                                bot_id: self.config.gateway.bot_id.clone(),
                                                trace_id: Some(upload.trace_id.clone()),
                                                utterance_id: identity.utterance_id.clone(),
                                                round_id: identity.round_id.clone(),
                                                playback_id: identity.playback_id.clone(),
                                                candidate_seq: probe.candidate_seq,
                                                speech_epoch: probe.speech_epoch,
                                                audio_watermark: probe.audio_watermark,
                                            })
                                            .await?;
                                            info!(
                                                utterance_id = %identity.utterance_id,
                                                candidate_seq = probe.candidate_seq,
                                                speech_epoch = probe.speech_epoch,
                                                audio_watermark = probe.audio_watermark,
                                                "[BARGE_IN] 候选探测已发送"
                                            );
                                        }
                                    }

                                    if barge_in_silence_started_at
                                        .map(|started| {
                                            started.elapsed() >= Duration::from_millis(300)
                                        })
                                        .unwrap_or(false)
                                    {
                                        self.cancel_barge_in_audio_stream(
                                            &conn,
                                            &mut barge_in_upload,
                                            &mut barge_in_schedule,
                                            &mut barge_in_frames,
                                            "speech_ended_without_commit",
                                        )
                                        .await;
                                        barge_in_silence_started_at = None;
                                        barge_in_last_ignored_seq = 0;
                                        trigger_window.clear();
                                        recording_preroll.clear();
                                    }
                                }
                                continue;
                            }
                            if Self::should_preserve_wake_prebuffer_during_playback(
                                self.config.audio.frontend_mode,
                                self.config.audio.frontend_mute_mic_during_playback,
                                collect_wake_capture_prebuffer,
                            ) {
                                Self::push_audio_prebuffer(
                                    &mut wake_capture_prebuffer,
                                    wake_capture_prebuffer_capacity,
                                    frame.clone(),
                                );
                            }
                            if !self.config.audio.should_detect_tts_barge_in() {
                                trigger_window.clear();
                                recording_preroll.clear();
                                continue;
                            }

                            let is_trigger_speech = vad
                                .is_start_trigger_speech(
                                    &frame,
                                    self.config.vad.speech_start_min_dbfs,
                                )
                                .unwrap_or(false);
                            Self::push_audio_prebuffer(
                                &mut recording_preroll,
                                wake_capture_prebuffer_capacity,
                                frame.clone(),
                            );
                            trigger_window.push(frame, is_trigger_speech);

                            if trigger_window.should_trigger() {
                                info!("[BARGE_IN] TTS 播放期间检测到用户语音，打断播放并开始录音");
                                Self::cancel_tts_generation(
                                    &playback.generation,
                                    &playback.command_tx,
                                    &tts_cmd_tx,
                                    "tts_barge_in_interrupt",
                                );
                                Self::set_audio_frontend_speaking(
                                    &mut audio_frontend_control,
                                    &mut audio_frontend_speaking,
                                    self.config.audio.frontend_mute_mic_during_playback,
                                    false,
                                    "tts_barge_in",
                                );
                                tts_accepting_audio.store(false, Ordering::Relaxed);
                                Self::reset_tts_opus_decoder(
                                    &mut tts_opus_decoder,
                                    "tts_barge_in_reset",
                                );
                                pending_exit = None;
                                pending_playback_state = None;
                                pending_playback_interrupt_reason =
                                    Some("tts_barge_in".to_string());
                                Self::remember_stale_playback_round(
                                    current_playback_round.as_ref(),
                                    &mut stale_playback_rounds,
                                    "tts_barge_in",
                                );
                                playback_deadline = None;
                                suppress_stale_server_messages = true;
                                let _ = conn
                                    .send_control(&ClientMessage::Interrupt {
                                        reason: Some("tts_barge_in".to_string()),
                                    })
                                    .await;
                                self.cancel_recording_audio_stream(
                                    &conn,
                                    &mut recording_upload,
                                    &mut recording_frames,
                                    "tts_barge_in",
                                )
                                .await;

                                recording_upload_seq = recording_upload_seq.wrapping_add(1);
                                recording_upload = Some(
                                    self.start_recording_audio_stream(&conn, recording_upload_seq)
                                        .await?,
                                );
                                recording_frames = if recording_preroll.is_empty() {
                                    trigger_window.drain_frames()
                                } else {
                                    trigger_window.clear();
                                    recording_preroll.drain(..).collect()
                                };
                                silence_started_at = None;
                                turn_candidate_seq = 0;
                                turn_candidate_speech_epoch = 0;
                                turn_candidate_sent_for_silence = false;
                                turn_candidate_continue_confirmed = false;
                                pending_turn_commit = None;
                                if let Some(upload) = recording_upload.as_mut() {
                                    self.flush_recording_audio_chunks(
                                        &conn,
                                        upload,
                                        &mut recording_frames,
                                        false,
                                    )
                                    .await?;
                                }
                                state = DialogueState::Recording {
                                    started_at: Instant::now(),
                                    last_interaction: Instant::now(),
                                };
                            }
                        }
                    },
                    Err(TryRecvError::Disconnected) => {
                        return Err(anyhow::anyhow!("音频帧通道已断开"));
                    }
                    Err(TryRecvError::Empty) => {}
                }
            }

            loop {
                match control_rx.try_recv() {
                    Ok(TransportEvent::Message(message)) => {
                        if let ServerMessage::Error { code, message } = &message {
                            if code == "REGISTER_FAILED" || code == "REGISTER_REQUIRED" {
                                warn!(
                                    code = %code,
                                    "[GATEWAY] 注册失败，关闭连接并重连: {message}"
                                );
                                return Err(anyhow::anyhow!(
                                    "Robot 注册未完成: {code} - {message}"
                                ));
                            }
                        }
                        if let ServerMessage::BargeInDecision {
                            trace_id,
                            utterance_id,
                            round_id,
                            playback_id,
                            candidate_seq,
                            speech_epoch,
                            audio_watermark,
                            decision,
                            reason,
                            asr_text,
                            asr_time_ms,
                        } = &message
                        {
                            if decision == "ignore" {
                                let upload_samples = barge_in_upload
                                    .as_ref()
                                    .map(|upload| upload.total_samples as u64)
                                    .unwrap_or_default();
                                let current_matches = barge_in_schedule
                                    .as_ref()
                                    .map(|schedule| {
                                        schedule.matches(
                                            utterance_id,
                                            round_id,
                                            playback_id,
                                            *candidate_seq,
                                            *speech_epoch,
                                            *audio_watermark,
                                            upload_samples,
                                        )
                                    })
                                    .unwrap_or(false);
                                if current_matches {
                                    barge_in_last_ignored_seq = *candidate_seq;
                                }
                                tracing::debug!(
                                    utterance_id,
                                    candidate_seq,
                                    reason = reason.as_deref().unwrap_or("ignored"),
                                    "[BARGE_IN] 候选判定为忽略，继续播放"
                                );
                                continue;
                            }
                            let upload_samples = barge_in_upload
                                .as_ref()
                                .map(|upload| upload.total_samples as u64)
                                .unwrap_or_default();
                            let candidate_matches = barge_in_schedule
                                .as_ref()
                                .map(|schedule| {
                                    schedule.matches(
                                        utterance_id,
                                        round_id,
                                        playback_id,
                                        *candidate_seq,
                                        *speech_epoch,
                                        *audio_watermark,
                                        upload_samples,
                                    )
                                })
                                .unwrap_or(false);
                            let playback_matches = current_playback_round
                                .as_ref()
                                .map(|round| {
                                    round.round_id == *round_id && round.playback_id == *playback_id
                                })
                                .unwrap_or(false);
                            let accepted = self.config.audio.should_detect_natural_barge_in()
                                && matches!(state, DialogueState::TtsPlaying)
                                && playback_matches
                                && candidate_matches
                                && matches!(decision.as_str(), "interrupt" | "new_intent");

                            if accepted {
                                info!(
                                    utterance_id,
                                    round_id,
                                    playback_id,
                                    candidate_seq,
                                    decision,
                                    asr_text = asr_text.as_deref().unwrap_or(""),
                                    asr_time_ms = asr_time_ms.unwrap_or_default(),
                                    "[BARGE_IN] ASR 已确认，执行自然打断"
                                );
                                Self::cancel_tts_generation(
                                    &playback.generation,
                                    &playback.command_tx,
                                    &tts_cmd_tx,
                                    "natural_barge_in_interrupt",
                                );
                                Self::set_audio_frontend_speaking(
                                    &mut audio_frontend_control,
                                    &mut audio_frontend_speaking,
                                    self.config.audio.frontend_mute_mic_during_playback,
                                    false,
                                    "natural_barge_in",
                                );
                                tts_accepting_audio.store(false, Ordering::Relaxed);
                                Self::reset_tts_opus_decoder(
                                    &mut tts_opus_decoder,
                                    "natural_barge_in_reset",
                                );
                                pending_exit = None;
                                pending_playback_state = None;
                                pending_playback_interrupt_reason =
                                    Some("natural_barge_in".to_string());
                                Self::remember_stale_playback_round(
                                    current_playback_round.as_ref(),
                                    &mut stale_playback_rounds,
                                    "natural_barge_in",
                                );
                                playback_deadline = None;
                                suppress_stale_server_messages = true;
                                let _ = conn
                                    .send_control(&ClientMessage::Interrupt {
                                        reason: Some("natural_barge_in".to_string()),
                                    })
                                    .await;
                            }

                            conn.send_control(&ClientMessage::BargeInCommitAck {
                                bot_id: self.config.gateway.bot_id.clone(),
                                trace_id: trace_id.clone(),
                                utterance_id: utterance_id.clone(),
                                round_id: round_id.clone(),
                                playback_id: playback_id.clone(),
                                candidate_seq: *candidate_seq,
                                speech_epoch: *speech_epoch,
                                audio_watermark: *audio_watermark,
                                accepted,
                                reason: if accepted {
                                    "client_committed".to_string()
                                } else {
                                    "stale_or_inactive_candidate".to_string()
                                },
                            })
                            .await?;

                            if accepted {
                                barge_in_upload = None;
                                barge_in_schedule = None;
                                barge_in_frames.clear();
                                barge_in_silence_started_at = None;
                                barge_in_last_ignored_seq = 0;
                                trigger_window.clear();
                                recording_preroll.clear();
                                state = if decision == "new_intent" {
                                    DialogueState::SendingAudio {
                                        last_interaction: Instant::now(),
                                        waiting_started_at: Instant::now(),
                                    }
                                } else {
                                    Self::awake_idle_state()
                                };
                            }
                            continue;
                        }
                        if let ServerMessage::TurnCandidateDecision {
                            utterance_id,
                            candidate_seq,
                            speech_epoch,
                            audio_watermark,
                            decision,
                            reason,
                            ..
                        } = &message
                        {
                            let current_matches = self.config.turn_gate_shadow.active_enabled
                                && decision == "continue"
                                && matches!(&state, DialogueState::Recording { .. })
                                && recording_upload
                                    .as_ref()
                                    .map(|upload| {
                                        upload.utterance_id == *utterance_id
                                            && *candidate_seq == turn_candidate_seq
                                            && *speech_epoch == turn_candidate_speech_epoch
                                            && *audio_watermark <= upload.total_samples as u64
                                            && turn_candidate_sent_for_silence
                                    })
                                    .unwrap_or(false);
                            if current_matches {
                                turn_candidate_continue_confirmed = true;
                                info!(
                                    utterance_id = %utterance_id,
                                    candidate_seq,
                                    speech_epoch,
                                    reason = reason.as_deref().unwrap_or("models_continue"),
                                    hard_timeout_ms = self.config.turn_gate_shadow.ambiguous_hard_timeout_ms,
                                    "[TURN_GATE][ACTIVE] 收到继续听反馈，延长静音窗口"
                                );
                            } else {
                                warn!(
                                    utterance_id = %utterance_id,
                                    candidate_seq,
                                    speech_epoch,
                                    decision = %decision,
                                    "[TURN_GATE][ACTIVE] 忽略过期或不匹配的候选反馈"
                                );
                            }
                            continue;
                        }
                        if let ServerMessage::TurnCommitRequest {
                            trace_id,
                            utterance_id,
                            candidate_seq,
                            speech_epoch,
                            audio_watermark,
                            asr_text,
                            asr_time_ms,
                        } = &message
                        {
                            let mut reason = "active_disabled";
                            if self.config.turn_gate_shadow.active_enabled {
                                reason = "stale_candidate";
                                let current_matches = recording_upload
                                    .as_ref()
                                    .map(|upload| {
                                        upload.utterance_id == *utterance_id
                                            && *candidate_seq == turn_candidate_seq
                                            && *speech_epoch == turn_candidate_speech_epoch
                                            && *audio_watermark <= upload.total_samples as u64
                                            && turn_candidate_sent_for_silence
                                            && silence_started_at.is_some()
                                    })
                                    .unwrap_or(false);
                                if current_matches
                                    && matches!(&state, DialogueState::Recording { .. })
                                {
                                    let silence_elapsed_ms = silence_started_at
                                        .map(|started| started.elapsed().as_millis() as u64)
                                        .unwrap_or_default();
                                    let wait_ms = Self::turn_commit_wait_ms(
                                        silence_elapsed_ms,
                                        self.config.turn_gate_shadow.min_commit_silence_ms,
                                    );
                                    pending_turn_commit = Some(PendingTurnCommit {
                                        trace_id: trace_id.clone(),
                                        utterance_id: utterance_id.clone(),
                                        candidate_seq: *candidate_seq,
                                        speech_epoch: *speech_epoch,
                                        audio_watermark: *audio_watermark,
                                        asr_text: asr_text.clone(),
                                        asr_time_ms: *asr_time_ms,
                                        received_at: Instant::now(),
                                    });
                                    info!(
                                        utterance_id = %utterance_id,
                                        candidate_seq,
                                        speech_epoch,
                                        audio_watermark,
                                        silence_elapsed_ms,
                                        min_commit_silence_ms = self.config.turn_gate_shadow.min_commit_silence_ms,
                                        wait_ms,
                                        "[TURN_GATE][ACTIVE] 云端提交请求已暂存，等待稳定静音确认"
                                    );
                                    continue;
                                }
                            }
                            conn.send_control(&ClientMessage::TurnCommitAck {
                                bot_id: self.config.gateway.bot_id.clone(),
                                trace_id: trace_id.clone(),
                                utterance_id: utterance_id.clone(),
                                candidate_seq: *candidate_seq,
                                speech_epoch: *speech_epoch,
                                audio_watermark: *audio_watermark,
                                accepted: false,
                                reason: reason.to_string(),
                            })
                            .await?;
                            continue;
                        }
                        let is_registered = matches!(&message, ServerMessage::Registered { .. });
                        let play_ready_prompt = match &message {
                            ServerMessage::Connected { .. } => false,
                            ServerMessage::Registered { .. } => true,
                            _ => false,
                        };
                        let is_error = matches!(&message, ServerMessage::Error { .. });
                        let is_playback_start =
                            matches!(&message, ServerMessage::PlaybackStart { .. });
                        let clears_pre_playback_rtp = matches!(
                            &message,
                            ServerMessage::Done { .. }
                                | ServerMessage::PlaybackCancel { .. }
                                | ServerMessage::Error { .. }
                        );
                        let done_exit = Self::done_without_playback_exit_candidate(
                            &message,
                            &state,
                            current_playback_round.as_ref(),
                            &stale_playback_rounds,
                            &stale_server_trace_ids,
                        );
                        let playback_generation_before = playback.generation.current();
                        state = self.handle_server_message(
                            message,
                            state,
                            &playback.command_tx,
                            &playback.generation,
                            &tts_cmd_tx,
                            &tts_accepting_audio,
                            &mut tts_opus_decoder,
                            &mut pending_exit,
                            &mut suppress_stale_server_messages,
                            &mut current_playback_round,
                            &mut stale_playback_rounds,
                            &stale_server_trace_ids,
                            &mut pending_response_trace_id,
                            &mut pending_playback_interrupt_reason,
                        );
                        if playback.generation.current() != playback_generation_before {
                            Self::set_audio_frontend_speaking(
                                &mut audio_frontend_control,
                                &mut audio_frontend_speaking,
                                self.config.audio.frontend_mute_mic_during_playback,
                                false,
                                "server_cancel",
                            );
                        }
                        if clears_pre_playback_rtp {
                            pending_pre_playback_rtp.clear();
                        } else if is_playback_start {
                            let (early_frames, dropped_frames) =
                                Self::take_pre_playback_rtp_for_round(
                                    &mut pending_pre_playback_rtp,
                                    current_playback_round.as_ref(),
                                    Instant::now(),
                                );
                            if !early_frames.is_empty() || dropped_frames > 0 {
                                info!(
                                    replayed_frames = early_frames.len(),
                                    dropped_frames, "[TTS] 处理 playback_start 前到达的 RTP 音频"
                                );
                            }
                            for early_frame in early_frames {
                                state = self.handle_server_audio_frame(
                                    early_frame,
                                    state,
                                    &tts_cmd_tx,
                                    &mut tts_opus_decoder,
                                    &mut current_playback_round,
                                    StalePlaybackFilter {
                                        rounds: &stale_playback_rounds,
                                        trace_ids: &stale_server_trace_ids,
                                    },
                                );
                            }
                        }
                        if is_error {
                            if let Some(next_state) = pending_playback_state.take() {
                                playback_deadline = None;
                                state = next_state;
                            }
                        }
                        if let Some(exit) = done_exit {
                            let (next_state, applied) = Self::state_after_done_without_playback(
                                state.clone(),
                                &mut pending_playback_state,
                                &mut pending_exit,
                                &mut current_playback_round,
                                exit,
                            );
                            if applied {
                                playback_deadline = None;
                                state = next_state;
                                match &state {
                                    DialogueState::WaitingForWakeWord => {
                                        info!("[STATE] 本轮无播放音频，等待唤醒...");
                                    }
                                    DialogueState::AwakeIdle { .. } => {
                                        info!("[AUDIO] 请说话...");
                                    }
                                    _ => {}
                                }
                            }
                        }
                        if is_registered {
                            session_ready = true;
                        }
                        if play_ready_prompt {
                            pending_playback_state = Some(DialogueState::WaitingForWakeWord);
                            let event_id = self
                                .send_client_event(&conn, "startup_ready", "startup_ready")
                                .await
                                .context("发送云端就绪播报失败")?;
                            pending_response_trace_id = Some(event_id);
                            info!("[WAKE] 已请求云端启动就绪播报: startup_ready");
                            playback_deadline = None;
                            state = DialogueState::SendingAudio {
                                last_interaction: Instant::now(),
                                waiting_started_at: Instant::now(),
                            };
                        }
                    }
                    Ok(TransportEvent::AudioFrame(frame)) => {
                        if suppress_stale_server_messages {
                            if !state.can_start_tts_playback()
                                || !Self::audio_frame_can_resume_after_stale_suppression(
                                    &frame,
                                    current_playback_round.as_ref(),
                                    StalePlaybackFilter {
                                        rounds: &stale_playback_rounds,
                                        trace_ids: &stale_server_trace_ids,
                                    },
                                )
                            {
                                continue;
                            }
                            suppress_stale_server_messages = false;
                        }
                        if Self::should_buffer_pre_playback_rtp(
                            &frame,
                            &state,
                            current_playback_round.as_ref(),
                        ) {
                            Self::buffer_pre_playback_rtp(
                                &mut pending_pre_playback_rtp,
                                frame,
                                playback.generation.current(),
                                Instant::now(),
                            );
                            continue;
                        }
                        state = self.handle_server_audio_frame(
                            frame,
                            state,
                            &tts_cmd_tx,
                            &mut tts_opus_decoder,
                            &mut current_playback_round,
                            StalePlaybackFilter {
                                rounds: &stale_playback_rounds,
                                trace_ids: &stale_server_trace_ids,
                            },
                        );
                    }
                    Ok(TransportEvent::Closed) => return Ok(()),
                    Ok(TransportEvent::Error(err)) => return Err(err),
                    Err(TokioTryRecvError::Empty) => break,
                    Err(TokioTryRecvError::Disconnected) => return Ok(()),
                }
            }

            if let Some(pending) = pending_turn_commit.clone() {
                let last_interaction = match &state {
                    DialogueState::Recording {
                        last_interaction, ..
                    } => Some(*last_interaction),
                    _ => None,
                };
                let current_matches = recording_upload
                    .as_ref()
                    .map(|upload| {
                        Self::pending_turn_commit_matches(
                            &pending,
                            &upload.utterance_id,
                            upload.total_samples as u64,
                            turn_candidate_seq,
                            turn_candidate_speech_epoch,
                            turn_candidate_sent_for_silence,
                        )
                    })
                    .unwrap_or(false);
                let silence_elapsed_ms = silence_started_at
                    .map(|started| started.elapsed().as_millis() as u64)
                    .unwrap_or_default();
                let wait_ms = Self::turn_commit_wait_ms(
                    silence_elapsed_ms,
                    self.config.turn_gate_shadow.min_commit_silence_ms,
                );

                if !current_matches || last_interaction.is_none() {
                    pending_turn_commit = None;
                    conn.send_control(&ClientMessage::TurnCommitAck {
                        bot_id: self.config.gateway.bot_id.clone(),
                        trace_id: pending.trace_id,
                        utterance_id: pending.utterance_id,
                        candidate_seq: pending.candidate_seq,
                        speech_epoch: pending.speech_epoch,
                        audio_watermark: pending.audio_watermark,
                        accepted: false,
                        reason: "stale_candidate".to_string(),
                    })
                    .await?;
                } else if wait_ms == 0 {
                    let _ = pending_turn_commit.take();
                    let upload = recording_upload.take().expect("validated recording upload");
                    if let Some(text) = pending
                        .asr_text
                        .as_deref()
                        .filter(|text| !text.trim().is_empty())
                    {
                        if let Some(ms) = pending.asr_time_ms {
                            info!("[ASR][TURN_GATE] {} ({:.0} ms)", text, ms);
                        } else {
                            info!("[ASR][TURN_GATE] {}", text);
                        }
                    }
                    Self::schedule_feedback(
                        playback.command_tx.clone(),
                        playback.generation.clone(),
                        asr_ding.as_deref(),
                        Duration::from_millis(150),
                        "Turn Gate 提交确认音",
                    );
                    pending_response_trace_id = pending.trace_id.clone();
                    recording_frames.clear();
                    recording_preroll.clear();
                    silence_started_at = None;
                    turn_candidate_seq = 0;
                    turn_candidate_speech_epoch = 0;
                    turn_candidate_sent_for_silence = false;
                    turn_candidate_continue_confirmed = false;
                    trigger_window.clear();
                    state = DialogueState::SendingAudio {
                        last_interaction: last_interaction.expect("validated recording state"),
                        waiting_started_at: Instant::now(),
                    };
                    conn.send_control(&ClientMessage::TurnCommitAck {
                        bot_id: self.config.gateway.bot_id.clone(),
                        trace_id: pending.trace_id,
                        utterance_id: pending.utterance_id.clone(),
                        candidate_seq: pending.candidate_seq,
                        speech_epoch: pending.speech_epoch,
                        audio_watermark: pending.audio_watermark,
                        accepted: true,
                        reason: "stable_silence_confirmed".to_string(),
                    })
                    .await?;
                    info!(
                        utterance_id = %pending.utterance_id,
                        candidate_seq = pending.candidate_seq,
                        speech_epoch = pending.speech_epoch,
                        audio_watermark = pending.audio_watermark,
                        uploaded_samples = upload.total_samples,
                        silence_elapsed_ms,
                        min_commit_silence_ms = self.config.turn_gate_shadow.min_commit_silence_ms,
                        guard_hold_ms = pending.received_at.elapsed().as_millis() as u64,
                        "[TURN_GATE][ACTIVE] 稳定静音已确认，首次双 True 提交生效"
                    );
                    continue;
                }
            }

            if let DialogueState::Recording {
                started_at,
                last_interaction,
            } = &state
            {
                let recording_limit =
                    Duration::from_secs(self.config.vad.max_recording_duration_secs);
                if started_at.elapsed() >= recording_limit {
                    warn!(
                        "[TIMEOUT] 录音状态超时 ({}s)，强制结束本轮录音",
                        self.config.vad.max_recording_duration_secs
                    );

                    let has_streamed_audio = recording_upload
                        .as_ref()
                        .map(|upload| upload.has_audio())
                        .unwrap_or(false);
                    if recording_frames.is_empty() && !has_streamed_audio {
                        self.cancel_recording_audio_stream(
                            &conn,
                            &mut recording_upload,
                            &mut recording_frames,
                            "recording_timeout_empty",
                        )
                        .await;
                        Self::reset_recording_state(
                            &mut trigger_window,
                            &mut recording_frames,
                            &mut silence_started_at,
                        );
                        info!("[AUDIO] 请说话...");
                        state = Self::awake_idle_state();
                        continue;
                    }

                    let last_interaction = *last_interaction;
                    let Some(mut upload) = recording_upload.take() else {
                        Self::reset_recording_state(
                            &mut trigger_window,
                            &mut recording_frames,
                            &mut silence_started_at,
                        );
                        info!("[AUDIO] 请说话...");
                        state = Self::awake_idle_state();
                        continue;
                    };
                    let samples_len = self
                        .finish_recording_audio_stream(&conn, &mut upload, &mut recording_frames)
                        .await?;
                    Self::schedule_feedback(
                        playback.command_tx.clone(),
                        playback.generation.clone(),
                        asr_ding.as_deref(),
                        Duration::from_millis(150),
                        "语音提交确认音",
                    );
                    info!(
                        "[REC] 已发送录音: chunks={} / {} 样本 / {:.2}s",
                        upload.total_chunks,
                        samples_len,
                        samples_len as f32 / self.config.audio.sample_rate as f32,
                    );

                    recording_frames.clear();
                    silence_started_at = None;
                    turn_candidate_seq = 0;
                    turn_candidate_speech_epoch = 0;
                    turn_candidate_sent_for_silence = false;
                    turn_candidate_continue_confirmed = false;
                    pending_turn_commit = None;
                    trigger_window.clear();
                    state = DialogueState::SendingAudio {
                        last_interaction,
                        waiting_started_at: Instant::now(),
                    };
                    continue;
                }
            }

            if let DialogueState::SendingAudio {
                waiting_started_at, ..
            } = &state
            {
                if waiting_started_at.elapsed()
                    >= Duration::from_secs(self.config.runtime.response_timeout_secs)
                {
                    warn!(
                        "[TIMEOUT] 等待响应超时 ({}s)，恢复到请说话状态",
                        self.config.runtime.response_timeout_secs
                    );
                    Self::cancel_tts_generation(
                        &playback.generation,
                        &playback.command_tx,
                        &tts_cmd_tx,
                        "response_timeout_interrupt",
                    );
                    Self::set_audio_frontend_speaking(
                        &mut audio_frontend_control,
                        &mut audio_frontend_speaking,
                        self.config.audio.frontend_mute_mic_during_playback,
                        false,
                        "response_timeout_interrupt",
                    );
                    tts_accepting_audio.store(false, Ordering::Relaxed);
                    Self::reset_tts_opus_decoder(&mut tts_opus_decoder, "response_timeout_reset");
                    pending_exit = None;
                    pending_playback_interrupt_reason = Some("response_timeout".to_string());
                    playback_deadline = None;
                    Self::remember_stale_playback_round(
                        current_playback_round.as_ref(),
                        &mut stale_playback_rounds,
                        "response_timeout",
                    );
                    Self::remember_stale_server_trace(
                        pending_response_trace_id.take(),
                        &mut stale_server_trace_ids,
                        "response_timeout",
                    );
                    suppress_stale_server_messages = true;
                    self.cancel_recording_audio_stream(
                        &conn,
                        &mut recording_upload,
                        &mut recording_frames,
                        "response_timeout",
                    )
                    .await;
                    Self::reset_recording_state(
                        &mut trigger_window,
                        &mut recording_frames,
                        &mut silence_started_at,
                    );
                    self.drain_capture_frames(capture.as_ref());
                    Self::clear_wake_audio_buffers(
                        &mut wake_capture_prebuffer,
                        &mut recording_preroll,
                        &mut collect_wake_capture_prebuffer,
                    );
                    let _ = conn
                        .send_control(&ClientMessage::Interrupt {
                            reason: Some("response_timeout".to_string()),
                        })
                        .await;
                    state = Self::state_after_response_timeout(&mut pending_playback_state);
                    if matches!(state, DialogueState::WaitingForWakeWord) {
                        self.pause_capture_for_waiting(&mut capture);
                        info!("[STATE] 响应超时，等待唤醒...");
                    } else {
                        info!("[AUDIO] 请说话...");
                    }
                    continue;
                }
            }

            if let Some((deadline, label)) = playback_deadline {
                if Instant::now() >= deadline {
                    warn!("[TIMEOUT] {}等待播放完成超时，重置本地播放状态", label);
                    Self::cancel_tts_generation(
                        &playback.generation,
                        &playback.command_tx,
                        &tts_cmd_tx,
                        "playback_timeout_interrupt",
                    );
                    Self::set_audio_frontend_speaking(
                        &mut audio_frontend_control,
                        &mut audio_frontend_speaking,
                        self.config.audio.frontend_mute_mic_during_playback,
                        false,
                        "playback_timeout_interrupt",
                    );
                    tts_accepting_audio.store(false, Ordering::Relaxed);
                    Self::reset_tts_opus_decoder(&mut tts_opus_decoder, "playback_timeout_reset");
                    pending_exit = None;
                    playback_deadline = None;
                    pending_playback_interrupt_reason = Some("playback_timeout".to_string());
                    Self::remember_stale_playback_round(
                        current_playback_round.as_ref(),
                        &mut stale_playback_rounds,
                        "playback_timeout",
                    );
                    self.cancel_recording_audio_stream(
                        &conn,
                        &mut recording_upload,
                        &mut recording_frames,
                        "playback_timeout",
                    )
                    .await;
                    Self::reset_recording_state(
                        &mut trigger_window,
                        &mut recording_frames,
                        &mut silence_started_at,
                    );
                    self.drain_capture_frames(capture.as_ref());
                    Self::clear_wake_audio_buffers(
                        &mut wake_capture_prebuffer,
                        &mut recording_preroll,
                        &mut collect_wake_capture_prebuffer,
                    );

                    if let Some(next_state) = pending_playback_state.take() {
                        state = next_state;
                    } else if matches!(state, DialogueState::TtsPlaying) {
                        let _ = conn
                            .send_control(&ClientMessage::Interrupt {
                                reason: Some("playback_timeout".to_string()),
                            })
                            .await;
                        info!("[AUDIO] 请说话...");
                        state = Self::awake_idle_state();
                    }
                    continue;
                }
            }

            if matches!(state, DialogueState::WaitingForWakeWord) {
                if !standby_notification_sent
                    && pending_playback_state.is_none()
                    && playback_deadline.is_none()
                {
                    self.notify_perception_start("standby");
                    standby_notification_sent = true;
                }
            } else {
                standby_notification_sent = false;
            }

            if !matches!(state, DialogueState::TtsPlaying) && barge_in_upload.is_some() {
                self.cancel_barge_in_audio_stream(
                    &conn,
                    &mut barge_in_upload,
                    &mut barge_in_schedule,
                    &mut barge_in_frames,
                    "playback_no_longer_active",
                )
                .await;
                barge_in_silence_started_at = None;
                barge_in_last_ignored_seq = 0;
            }

            if self.config.audio.should_pause_capture_while_waiting()
                && matches!(state, DialogueState::WaitingForWakeWord)
            {
                self.pause_capture_for_waiting(&mut capture);
            }

            if last_heartbeat.elapsed()
                >= Duration::from_secs(self.config.gateway.heartbeat_interval_secs)
            {
                conn.send_control(&ClientMessage::Heartbeat).await?;
                last_heartbeat = Instant::now();
            }

            let loop_sleep = if matches!(state, DialogueState::WaitingForWakeWord)
                && capture.is_none()
                && pending_playback_state.is_none()
                && playback_deadline.is_none()
            {
                Duration::from_millis(50)
            } else {
                Duration::from_millis(5)
            };
            time::sleep(loop_sleep).await;
        }
    }

    fn log_config(&self) {
        info!("Gateway: {}", self.config.gateway.url);
        info!("Bot ID: {}", self.config.gateway.bot_id);
        info!(
            "[ROBOT] robot_id={}, robot_secret={}",
            self.config.gateway.robot_id.as_deref().unwrap_or("<none>"),
            if self.config.gateway.robot_secret.is_some() {
                "<set>"
            } else {
                "<none>"
            },
        );
        if self.config.environment_trigger.template != "请根据环境主动回应" {
            let template = &self.config.environment_trigger.template;
            tracing::warn!(
                "ENV_TALK_TEMPLATE 已弃用，模板固定为「请根据以下环境主动回应：<content>」；当前值「{template}」将被忽略",
            );
        }
        info!(
            "Gateway timeout: connect={:.1}s, send={:.1}s",
            self.config.gateway.connect_timeout_secs, self.config.gateway.send_timeout_secs,
        );
        info!(
            "Transport: policy={}, webrtc_enabled={}, webrtc_connect_timeout_ms={}, fallback_enabled={}, rtc_audio_uplink={}",
            self.config.transport.policy.as_str(),
            self.config.transport.webrtc_enabled,
            self.config.transport.webrtc_connect_timeout_ms,
            self.config.transport.fallback_enabled,
            self.config.transport.webrtc_audio_uplink_mode.as_str(),
        );
        info!(
            "Wake source: mode={}, debounce={:.2}s",
            self.config.wake.source_mode.as_str(),
            self.config.wake.debounce_secs,
        );
        info!(
            "Wake serial: enabled={}, port={}, baud={}, timeout={:.2}s, reconnect={:.2}s",
            self.config.wake.source_mode.hardware_enabled(),
            self.config
                .wake
                .serial_port
                .as_deref()
                .unwrap_or("<auto-scan>"),
            self.config.wake.serial_baud,
            self.config.wake.serial_timeout_secs,
            self.config.wake.serial_reconnect_secs,
        );
        info!(
            "Wake HTTP: enabled={}, url=http://{}:{}{}",
            self.config.wake.source_mode.hardware_enabled() && self.config.wake.http_enabled,
            self.config.wake.http_host,
            self.config.wake.http_port,
            self.config.wake.http_path,
        );
        info!(
            "Wake KWS TCP: enabled={}, address={}:{} reconnect={:.2}s",
            self.config.wake.source_mode.kws_enabled(),
            self.config.wake.kws_host,
            self.config.wake.kws_port,
            self.config.wake.kws_reconnect_secs,
        );
        info!(
            "Audio: playback_buffer={} frames @ {}Hz, frontend_mode={}, audio_frontend={}:{}->{}, control_port={}, mute_mic_during_playback={}, tts_barge_in_with_aec={}, effective_tts_barge_in={}, pause_capture_while_waiting={}, effective_pause_capture_while_waiting={}, wake_capture_prebuffer_ms={}",
            self.config.audio.playback_buffer_frames,
            self.config.audio.sample_rate,
            self.config.audio.frontend_mode.as_str(),
            self.config.audio.frontend_host,
            self.config.audio.frontend_mic_port,
            self.config.audio.frontend_speaker_port,
            self.config.audio.frontend_control_port,
            self.config.audio.frontend_mute_mic_during_playback,
            self.config.audio.tts_barge_in_with_aec,
            self.config.audio.should_detect_tts_barge_in(),
            self.config.audio.pause_capture_while_waiting,
            self.config.audio.should_pause_capture_while_waiting(),
            self.config.audio.wake_capture_prebuffer_ms,
        );
        info!(
            "Opus: bitrate={}bps, frame=20ms, sample_rate={}Hz",
            configured_opus_bitrate_bps(),
            self.config.audio.sample_rate,
        );
        info!(
            "VAD: mode={}, speech={:.2}s, ratio={:.2}, start_min={:.1}dBFS, silence={:.2}s, max={}s",
            self.config.vad.mode,
            self.config.vad.speech_threshold_secs,
            self.config.vad.speech_trigger_ratio,
            self.config.vad.speech_start_min_dbfs,
            self.config.vad.silence_threshold_secs,
            self.config.vad.max_recording_duration_secs,
        );
        info!(
            "Turn Gate: enabled={}, active={}, candidate_silence={}ms, min_commit_silence={}ms, failure_fallback={}ms, ambiguous_hard_timeout={}ms",
            self.config.turn_gate_shadow.enabled,
            self.config.turn_gate_shadow.active_enabled,
            self.config.turn_gate_shadow.candidate_silence_ms,
            self.config.turn_gate_shadow.min_commit_silence_ms,
            self.config.turn_gate_shadow.failure_fallback_ms,
            self.config.turn_gate_shadow.ambiguous_hard_timeout_ms,
        );
        info!(
            "Runtime: session_timeout={}s, response_timeout={}s, tts_playback_timeout={}s",
            self.config.runtime.session_timeout_secs,
            self.config.runtime.response_timeout_secs,
            self.config.runtime.tts_playback_timeout_secs,
        );
        info!(
            "TTS: prebuffer={:.2}s, gain={:.1}, agc_target={:.1}",
            self.config.tts.prebuffer_secs,
            self.config.tts.volume_gain,
            self.config.tts.agc_target_db,
        );
        info!(
            "Audio ducking control: http://{}:{}, default_ttl={}ms, max_ttl={}ms",
            self.config.audio_control.host,
            self.config.audio_control.port,
            self.config.audio_control.default_ttl_ms,
            self.config.audio_control.max_ttl_ms,
        );
        info!(
            "Environment trigger: http://{}:{}, path={}, debounce={:.2}s, max_content_chars={}, perception_start_url={}",
            self.config.environment_trigger.host,
            self.config.environment_trigger.port,
            self.config.environment_trigger.path,
            self.config.environment_trigger.debounce_secs,
            self.config.environment_trigger.max_content_chars,
            self.config
                .environment_trigger
                .perception_start_url
                .as_deref()
                .unwrap_or("<disabled>"),
        );
        info!(
            "ASR ding: {}",
            self.config
                .feedback
                .asr_ding_sound_path
                .as_deref()
                .unwrap_or("<disabled>"),
        );
        info!(
            "Angle report: url={}, timeout={:.2}s",
            self.config.wake.angle_report_url, self.config.wake.angle_report_timeout_secs,
        );
    }

    fn try_send_tts_command(
        tts_cmd_tx: &TtsProcessorHandle,
        command: TtsProcessorCommand,
        context: &str,
    ) {
        if let Err(err) = tts_cmd_tx.try_send(command) {
            warn!(
                context,
                "[TTS][WARN] TTS 处理队列提交失败，命令已丢弃: {}", err
            );
        }
    }

    fn reset_tts_opus_decoder(decoder: &mut OpusStreamDecoder, context: &str) {
        if let Err(err) = decoder.reset() {
            warn!(
                context,
                "[TTS][WARN] TTS Opus 解码器重置失败，后续音频可能有毛刺: {}", err
            );
        }
    }

    fn cancel_tts_generation(
        playback_generation: &PlaybackGeneration,
        playback_tx: &CrossbeamSender<PlaybackCommand>,
        tts_cmd_tx: &TtsProcessorHandle,
        context: &str,
    ) {
        let (_, new_generation) = playback_generation.advance();
        Self::try_send_tts_command(tts_cmd_tx, TtsProcessorCommand::Reset, context);
        Self::try_send_playback_command(
            playback_tx,
            PlaybackCommand::Interrupt {
                generation: new_generation,
            },
            context,
        );
    }

    fn try_send_playback_command(
        playback_tx: &CrossbeamSender<PlaybackCommand>,
        command: PlaybackCommand,
        context: &str,
    ) {
        let send_result = match command {
            PlaybackCommand::MarkDone { generation } => playback_tx
                .send_timeout(
                    PlaybackCommand::MarkDone { generation },
                    Duration::from_secs(2),
                )
                .map_err(|err| err.to_string()),
            other => playback_tx.try_send(other).map_err(|err| err.to_string()),
        };
        if let Err(err) = send_result {
            warn!(
                context,
                "[PLAY][WARN] 播放队列提交失败，命令已丢弃: {}", err
            );
        }
    }

    fn playback_round_matches(
        playback_round: &PlaybackRound,
        round_id: Option<&str>,
        playback_id: Option<&str>,
    ) -> bool {
        let round_matches = round_id
            .map(|round_id| round_id == playback_round.round_id)
            .unwrap_or(true);
        let playback_matches = playback_id
            .map(|playback_id| playback_id == playback_round.playback_id)
            .unwrap_or(true);
        round_matches && playback_matches
    }

    fn classify_playback_event(
        current_playback_round: Option<&PlaybackRound>,
        event_generation: u64,
        current_generation: u64,
    ) -> PlaybackEventDisposition {
        let Some(round) = current_playback_round else {
            return PlaybackEventDisposition::Stale;
        };
        if round.generation != event_generation {
            PlaybackEventDisposition::Stale
        } else if event_generation == current_generation {
            PlaybackEventDisposition::Current
        } else {
            PlaybackEventDisposition::Cancelled
        }
    }

    fn remember_stale_playback_round(
        playback_round: Option<&PlaybackRound>,
        stale_playback_rounds: &mut Vec<PlaybackRoundKey>,
        reason: &str,
    ) {
        let Some(playback_round) = playback_round else {
            return;
        };
        let key = PlaybackRoundKey::from(playback_round);
        if stale_playback_rounds.contains(&key) {
            return;
        }
        info!(
            round_id = %key.round_id,
            playback_id = %key.playback_id,
            reason,
            "[ROUND] 标记过期播放轮次"
        );
        stale_playback_rounds.push(key);
        if stale_playback_rounds.len() > MAX_STALE_PLAYBACK_ROUNDS {
            stale_playback_rounds.remove(0);
        }
    }

    fn stale_playback_round_matches(
        stale_playback_rounds: &[PlaybackRoundKey],
        round_id: Option<&str>,
        playback_id: Option<&str>,
    ) -> bool {
        let Some(round_id) = round_id else {
            return false;
        };
        stale_playback_rounds.iter().any(|stale| {
            stale.round_id == round_id
                && playback_id
                    .map(|playback_id| playback_id == stale.playback_id)
                    .unwrap_or(true)
        })
    }

    fn remember_stale_server_trace(
        trace_id: Option<String>,
        stale_server_trace_ids: &mut Vec<String>,
        reason: &str,
    ) {
        let Some(trace_id) = trace_id else {
            return;
        };
        if trace_id.trim().is_empty() || stale_server_trace_ids.contains(&trace_id) {
            return;
        }
        info!(trace_id = %trace_id, reason, "[ROUND] 标记过期服务响应 trace");
        stale_server_trace_ids.push(trace_id);
        if stale_server_trace_ids.len() > MAX_STALE_SERVER_TRACES {
            stale_server_trace_ids.remove(0);
        }
    }

    fn stale_server_trace_matches(
        stale_server_trace_ids: &[String],
        trace_id: Option<&str>,
    ) -> bool {
        let Some(trace_id) = trace_id else {
            return false;
        };
        stale_server_trace_ids
            .iter()
            .any(|stale_trace_id| stale_trace_id == trace_id)
    }

    fn server_message_trace_id(message: &ServerMessage) -> Option<&str> {
        match message {
            ServerMessage::Status { trace_id, .. }
            | ServerMessage::Text { trace_id, .. }
            | ServerMessage::ResponseAsr { trace_id, .. }
            | ServerMessage::Done { trace_id, .. }
            | ServerMessage::PlaybackStart { trace_id, .. }
            | ServerMessage::PlaybackCancel { trace_id, .. } => trace_id.as_deref(),
            _ => None,
        }
    }

    fn server_message_stale_playback_round_matches(
        message: &ServerMessage,
        stale_playback_rounds: &[PlaybackRoundKey],
    ) -> bool {
        let (round_id, playback_id) = match message {
            ServerMessage::PlaybackStart {
                round_id,
                playback_id,
                ..
            } => (Some(round_id.as_str()), Some(playback_id.as_str())),
            ServerMessage::PlaybackCancel {
                round_id,
                playback_id,
                ..
            }
            | ServerMessage::Done {
                round_id,
                playback_id,
                ..
            } => (round_id.as_deref(), playback_id.as_deref()),
            _ => (None, None),
        };
        Self::stale_playback_round_matches(stale_playback_rounds, round_id, playback_id)
    }

    fn stale_audio_frame_matches(
        frame: &AudioFrame,
        current_playback_round: Option<&PlaybackRound>,
        stale_filter: StalePlaybackFilter<'_>,
    ) -> bool {
        if Self::stale_server_trace_matches(
            stale_filter.trace_ids,
            frame.header.trace_id.as_deref(),
        ) {
            return true;
        }
        if Self::stale_playback_round_matches(
            stale_filter.rounds,
            frame.header.round_id.as_deref(),
            frame.header.playback_id.as_deref(),
        ) {
            return true;
        }
        let Some(current_playback_round) = current_playback_round else {
            return false;
        };
        if frame.header.round_id.is_none() && frame.header.playback_id.is_none() {
            if let Some(start_sequence) = current_playback_round.rtp_start_sequence {
                let Some(sequence) = frame.header.seq.or(frame.header.chunk_seq) else {
                    return true;
                };
                if Self::rtp_sequence_precedes(sequence as u16, start_sequence) {
                    return true;
                }
            }
        }
        frame.header.round_id.is_none()
            && frame.header.playback_id.is_none()
            && stale_filter
                .rounds
                .iter()
                .any(|stale| stale == &PlaybackRoundKey::from(current_playback_round))
    }

    fn rtp_sequence_precedes(sequence: u16, start_sequence: u16) -> bool {
        sequence.wrapping_sub(start_sequence) >= 1 << 15
    }

    fn should_buffer_pre_playback_rtp(
        frame: &AudioFrame,
        state: &DialogueState,
        current_playback_round: Option<&PlaybackRound>,
    ) -> bool {
        current_playback_round.is_none()
            && state.can_start_tts_playback()
            && frame.header.direction.as_deref() == Some("server_tts")
            && frame.header.encoding == "opus"
            && frame.header.round_id.is_none()
            && frame.header.playback_id.is_none()
    }

    fn buffer_pre_playback_rtp(
        pending: &mut VecDeque<PendingPrePlaybackRtp>,
        frame: AudioFrame,
        generation: u64,
        now: Instant,
    ) {
        pending.retain(|item| {
            item.generation == generation
                && now.saturating_duration_since(item.received_at) <= PRE_PLAYBACK_RTP_TTL
        });
        while pending.len() >= PRE_PLAYBACK_RTP_MAX_FRAMES {
            pending.pop_front();
        }
        pending.push_back(PendingPrePlaybackRtp {
            received_at: now,
            generation,
            frame,
        });
    }

    fn take_pre_playback_rtp_for_round(
        pending: &mut VecDeque<PendingPrePlaybackRtp>,
        playback_round: Option<&PlaybackRound>,
        now: Instant,
    ) -> (Vec<AudioFrame>, usize) {
        let Some(playback_round) = playback_round else {
            let dropped = pending.len();
            pending.clear();
            return (Vec::new(), dropped);
        };
        let total = pending.len();
        let mut frames = pending
            .drain(..)
            .filter(|item| {
                item.generation == playback_round.generation
                    && now.saturating_duration_since(item.received_at) <= PRE_PLAYBACK_RTP_TTL
            })
            .map(|item| item.frame)
            .collect::<Vec<_>>();
        if let Some(start_sequence) = playback_round.rtp_start_sequence {
            frames.sort_by_key(|frame| {
                frame
                    .header
                    .seq
                    .or(frame.header.chunk_seq)
                    .map(|sequence| (sequence as u16).wrapping_sub(start_sequence))
                    .unwrap_or(u16::MAX)
            });
            frames.retain(|frame| {
                frame
                    .header
                    .seq
                    .or(frame.header.chunk_seq)
                    .is_some_and(|sequence| {
                        !Self::rtp_sequence_precedes(sequence as u16, start_sequence)
                    })
            });
        }
        let dropped = total.saturating_sub(frames.len());
        (frames, dropped)
    }

    fn audio_frame_can_resume_after_stale_suppression(
        frame: &AudioFrame,
        current_playback_round: Option<&PlaybackRound>,
        stale_filter: StalePlaybackFilter<'_>,
    ) -> bool {
        if Self::stale_audio_frame_matches(frame, current_playback_round, stale_filter) {
            return false;
        }
        !(current_playback_round.is_none()
            && frame.header.round_id.is_none()
            && frame.header.playback_id.is_none())
    }

    #[allow(clippy::too_many_arguments)]
    fn handle_server_message(
        &self,
        message: ServerMessage,
        state: DialogueState,
        playback_tx: &CrossbeamSender<PlaybackCommand>,
        playback_generation: &PlaybackGeneration,
        tts_cmd_tx: &TtsProcessorHandle,
        tts_accepting_audio: &Arc<AtomicBool>,
        tts_opus_decoder: &mut OpusStreamDecoder,
        pending_exit: &mut Option<bool>,
        suppress_stale_server_messages: &mut bool,
        current_playback_round: &mut Option<PlaybackRound>,
        stale_playback_rounds: &mut Vec<PlaybackRoundKey>,
        stale_server_trace_ids: &[String],
        pending_response_trace_id: &mut Option<String>,
        pending_playback_interrupt_reason: &mut Option<String>,
    ) -> DialogueState {
        if Self::stale_server_trace_matches(
            stale_server_trace_ids,
            Self::server_message_trace_id(&message),
        ) {
            info!(
                trace_id = Self::server_message_trace_id(&message).unwrap_or("-"),
                "[ROUND] 忽略过期服务响应"
            );
            return state;
        }

        if *suppress_stale_server_messages {
            match &message {
                ServerMessage::HeartbeatAck => return state,
                ServerMessage::PlaybackStart { .. }
                | ServerMessage::PlaybackCancel { .. }
                | ServerMessage::Done { .. }
                    if Self::server_message_stale_playback_round_matches(
                        &message,
                        stale_playback_rounds,
                    ) =>
                {
                    return state;
                }
                ServerMessage::Error { code, .. } if state.can_start_tts_playback() => {
                    info!(
                        code = %code,
                        "[ROUND] 忽略打断后未归属轮次的错误响应"
                    );
                    return state;
                }
                ServerMessage::Status { .. }
                | ServerMessage::Text { .. }
                | ServerMessage::ResponseAsr { .. }
                | ServerMessage::PlaybackStart { .. }
                | ServerMessage::Done { .. }
                    if state.can_start_tts_playback() =>
                {
                    *suppress_stale_server_messages = false;
                }
                _ => return state,
            }
        }

        match message {
            ServerMessage::Connected { session_id } => {
                info!(session_id = %session_id, "[GATEWAY] 已建立会话");
                state
            }
            ServerMessage::Registered {
                robot_id,
                bot_id,
                bot_name,
                ..
            } => {
                info!(
                    robot_id = %robot_id,
                    bot_id = %bot_id,
                    bot_name = bot_name.as_deref().unwrap_or("-"),
                    "[ROBOT] Robot 已注册"
                );
                state
            }
            ServerMessage::Status { message, .. } => {
                info!("[STATUS] {}", message);
                if message.contains("思考中") {
                    tts_accepting_audio.store(true, Ordering::Relaxed);
                    Self::reset_tts_opus_decoder(tts_opus_decoder, "status_thinking_start_round");
                    Self::try_send_tts_command(
                        tts_cmd_tx,
                        TtsProcessorCommand::StartRound,
                        "status_thinking_start_round",
                    );
                }
                if message.contains("未检测到有效语音") {
                    tts_accepting_audio.store(false, Ordering::Relaxed);
                    Self::reset_tts_opus_decoder(tts_opus_decoder, "no_valid_speech_reset");
                    Self::cancel_tts_generation(
                        playback_generation,
                        playback_tx,
                        tts_cmd_tx,
                        "no_valid_speech_reset",
                    );
                    return match state {
                        DialogueState::SendingAudio {
                            last_interaction, ..
                        } => DialogueState::AwakeIdle { last_interaction },
                        other => other,
                    };
                }
                state
            }
            ServerMessage::Text {
                content,
                asr_time_ms,
                asr_metadata: _,
                ..
            } => {
                if let Some(ms) = asr_time_ms {
                    info!("[ASR] {} ({:.0} ms)", content, ms);
                } else {
                    info!("[TEXT] {}", content);
                }
                state
            }
            ServerMessage::ResponseAsr { payload, .. } => {
                if payload.valid && payload.final_result && !payload.text.trim().is_empty() {
                    if let Some(ms) = payload.asr_time_ms {
                        info!("[ASR] {} ({:.0} ms)", payload.text, ms);
                    } else {
                        info!("[ASR] {}", payload.text);
                    }
                } else {
                    info!(
                        "[ASR] 收到无效或非最终识别结果: valid={}, final={}",
                        payload.valid, payload.final_result
                    );
                }
                state
            }
            ServerMessage::Done {
                exit,
                trace_id,
                round_id,
                playback_id,
            } => {
                if Self::stale_playback_round_matches(
                    stale_playback_rounds,
                    round_id.as_deref(),
                    playback_id.as_deref(),
                ) {
                    info!(
                        round_id = round_id.as_deref().unwrap_or("-"),
                        playback_id = playback_id.as_deref().unwrap_or("-"),
                        "[ROUND] 忽略过期 done"
                    );
                    return state;
                }
                if !Self::done_matches_current_playback_round(
                    current_playback_round.as_ref(),
                    round_id.as_deref(),
                    playback_id.as_deref(),
                ) {
                    info!(
                        done_round_id = round_id.as_deref().unwrap_or("-"),
                        done_playback_id = playback_id.as_deref().unwrap_or("-"),
                        current_round_id = current_playback_round
                            .as_ref()
                            .map(|round| round.round_id.as_str())
                            .unwrap_or("-"),
                        current_playback_id = current_playback_round
                            .as_ref()
                            .map(|round| round.playback_id.as_str())
                            .unwrap_or("-"),
                        "[ROUND] 忽略非当前轮次 done"
                    );
                    return state;
                }
                if !state.accepts_server_round_messages() {
                    if exit && matches!(state, DialogueState::AwakeIdle { .. }) {
                        info!(exit = exit, "[ROUND] 延迟 done 要求退出，等待唤醒");
                        *pending_exit = None;
                        return DialogueState::WaitingForWakeWord;
                    }
                    info!(exit = exit, "[ROUND] 忽略过期 done");
                    return state;
                }

                info!(exit = exit, "[ROUND] 本轮响应结束");
                if trace_id.as_ref() == pending_response_trace_id.as_ref() {
                    *pending_response_trace_id = None;
                }
                tts_accepting_audio.store(false, Ordering::Relaxed);
                Self::try_send_tts_command(
                    tts_cmd_tx,
                    TtsProcessorCommand::Finish,
                    "round_done_finish",
                );
                *pending_exit = Some(exit);
                state
            }
            ServerMessage::PlaybackStart {
                trace_id,
                round_id,
                playback_id,
                rtp_start_sequence,
            } => {
                if Self::stale_playback_round_matches(
                    stale_playback_rounds,
                    Some(&round_id),
                    Some(&playback_id),
                ) {
                    info!(
                        round_id = %round_id,
                        playback_id = %playback_id,
                        "[ROUND] 忽略过期播放轮次开始"
                    );
                    return state;
                }
                info!(
                    round_id = %round_id,
                    playback_id = %playback_id,
                    "[ROUND] 播放轮次开始"
                );
                if state.can_start_tts_playback() {
                    if trace_id.as_ref() == pending_response_trace_id.as_ref() {
                        *pending_response_trace_id = None;
                    }
                    *current_playback_round = Some(PlaybackRound {
                        round_id: round_id.clone(),
                        playback_id: playback_id.clone(),
                        generation: playback_generation.current(),
                        rtp_start_sequence,
                        first_audio_received_at: None,
                        playback_started_at: None,
                    });
                    *pending_playback_interrupt_reason = None;
                    tts_accepting_audio.store(true, Ordering::Relaxed);
                    Self::reset_tts_opus_decoder(tts_opus_decoder, "playback_start_round");
                    Self::try_send_tts_command(
                        tts_cmd_tx,
                        TtsProcessorCommand::StartRound,
                        "playback_start_round",
                    );
                }
                state
            }
            ServerMessage::PlaybackCancel {
                trace_id: _,
                round_id,
                playback_id,
                reason,
            } => {
                if let Some(playback_round) = current_playback_round.as_ref() {
                    if !Self::playback_round_matches(
                        playback_round,
                        round_id.as_deref(),
                        playback_id.as_deref(),
                    ) {
                        info!(
                            cancel_round_id = round_id.as_deref().unwrap_or("-"),
                            cancel_playback_id = playback_id.as_deref().unwrap_or("-"),
                            current_round_id = %playback_round.round_id,
                            current_playback_id = %playback_round.playback_id,
                            "[ROUND] 忽略过期播放取消"
                        );
                        return state;
                    }
                }
                info!(
                    round_id = round_id.as_deref().unwrap_or("-"),
                    playback_id = playback_id.as_deref().unwrap_or("-"),
                    reason = reason.as_deref().unwrap_or("-"),
                    "[ROUND] 收到播放取消"
                );
                Self::remember_stale_playback_round(
                    current_playback_round.as_ref(),
                    stale_playback_rounds,
                    reason.as_deref().unwrap_or("playback_cancel"),
                );
                tts_accepting_audio.store(false, Ordering::Relaxed);
                *pending_playback_interrupt_reason = Some(
                    reason
                        .clone()
                        .unwrap_or_else(|| "playback_cancel".to_string()),
                );
                Self::reset_tts_opus_decoder(tts_opus_decoder, "playback_cancel_reset");
                Self::cancel_tts_generation(
                    playback_generation,
                    playback_tx,
                    tts_cmd_tx,
                    "playback_cancel_reset",
                );
                *pending_exit = None;
                *suppress_stale_server_messages = true;
                Self::state_after_playback_cancel(state)
            }
            ServerMessage::Error { code, message } => {
                warn!(code = %code, "[GATEWAY][ERROR] {}", message);
                tts_accepting_audio.store(false, Ordering::Relaxed);
                Self::reset_tts_opus_decoder(tts_opus_decoder, "server_error_reset");
                Self::cancel_tts_generation(
                    playback_generation,
                    playback_tx,
                    tts_cmd_tx,
                    "server_error_reset",
                );
                *pending_exit = None;
                match state {
                    DialogueState::TtsPlaying => {
                        *pending_playback_interrupt_reason = Some(format!("server_error:{code}"));
                        Self::awake_idle_state()
                    }
                    DialogueState::SendingAudio { .. } => Self::awake_idle_state(),
                    other => other,
                }
            }
            ServerMessage::RtcConfig(config) => {
                info!(
                    session_id = %config.session_id,
                    ice_servers = config.ice_servers.len(),
                    codec = %config.media.audio.codec,
                    sample_rate = config.media.audio.sample_rate,
                    "[RTC] 收到 WebRTC 配置（当前媒体 transport 尚未启用）"
                );
                state
            }
            ServerMessage::RtcAnswer(answer) => {
                info!(
                    session_id = %answer.session_id,
                    sdp_bytes = answer.sdp.len(),
                    "[RTC] 收到 WebRTC answer（当前媒体 transport 尚未启用）"
                );
                state
            }
            ServerMessage::RtcIceCandidate(candidate) => {
                tracing::debug!(
                    session_id = %candidate.session_id,
                    sdp_mid = candidate.sdp_mid.as_deref().unwrap_or("-"),
                    sdp_mline_index = candidate.sdp_mline_index.unwrap_or_default(),
                    "[RTC] 收到 ICE candidate（当前媒体 transport 尚未启用）"
                );
                state
            }
            ServerMessage::TransportFallbackAck(ack) => {
                info!(
                    session_id = %ack.session_id,
                    active_transport = %ack.active_transport,
                    reason = ack.reason.as_deref().unwrap_or("-"),
                    "[TRANSPORT] 收到 fallback ack"
                );
                state
            }
            ServerMessage::TurnCommitRequest { .. }
            | ServerMessage::TurnCandidateDecision { .. }
            | ServerMessage::BargeInDecision { .. } => state,
            ServerMessage::HeartbeatAck => state,
            ServerMessage::Pong => state,
        }
    }

    fn handle_server_audio_frame(
        &self,
        frame: AudioFrame,
        state: DialogueState,
        tts_cmd_tx: &TtsProcessorHandle,
        tts_opus_decoder: &mut OpusStreamDecoder,
        current_playback_round: &mut Option<PlaybackRound>,
        stale_filter: StalePlaybackFilter<'_>,
    ) -> DialogueState {
        if !state.accepts_server_round_messages() {
            warn!("[TTS][WARN] 收到非当前轮次的二进制音频，已忽略");
            return state;
        }
        if frame.header.direction.as_deref() != Some("server_tts") {
            warn!(
                direction = frame.header.direction.as_deref().unwrap_or("-"),
                "[TTS][WARN] 收到非 server_tts 二进制音频帧，已忽略"
            );
            return state;
        }
        if frame.header.encoding != "opus" {
            warn!(
                encoding = %frame.header.encoding,
                "[TTS][WARN] 收到不支持的二进制音频编码，已忽略"
            );
            return state;
        }
        if let Some(sample_rate) = frame.header.sample_rate {
            if sample_rate != self.config.audio.tts_source_rate {
                warn!(
                    sample_rate,
                    expected = self.config.audio.tts_source_rate,
                    "[TTS][WARN] 二进制 Opus 采样率与配置不一致，已忽略"
                );
                return state;
            }
        }
        if let Some(seq) = frame.header.chunk_seq.or(frame.header.seq) {
            tracing::debug!(
                round_id = frame.header.round_id.as_deref().unwrap_or("-"),
                playback_id = frame.header.playback_id.as_deref().unwrap_or("-"),
                chunk_seq = seq,
                bytes = frame.payload.len(),
                "[TTS] 收到二进制 Opus 音频块"
            );
        }
        let chunk_seq = frame.header.chunk_seq.or(frame.header.seq);
        let payload_bytes = frame.payload.len();
        if Self::stale_audio_frame_matches(&frame, current_playback_round.as_ref(), stale_filter) {
            warn!(
                frame_round_id = frame.header.round_id.as_deref().unwrap_or("-"),
                frame_playback_id = frame.header.playback_id.as_deref().unwrap_or("-"),
                current_round_id = current_playback_round
                    .as_ref()
                    .map(|round| round.round_id.as_str())
                    .unwrap_or("-"),
                current_playback_id = current_playback_round
                    .as_ref()
                    .map(|round| round.playback_id.as_str())
                    .unwrap_or("-"),
                chunk_seq = chunk_seq.unwrap_or(0),
                bytes = payload_bytes,
                "[TTS][WARN] 收到已取消/过期轮次音频块，已丢弃"
            );
            return state;
        }
        if current_playback_round.is_none()
            && frame.header.round_id.is_none()
            && frame.header.playback_id.is_none()
        {
            warn!(
                chunk_seq = chunk_seq.unwrap_or(0),
                bytes = payload_bytes,
                "[TTS][WARN] 收到无当前播放轮次的 RTP 音频块，已丢弃"
            );
            return state;
        }
        if let Some(playback_round) = current_playback_round.as_ref() {
            if !Self::playback_round_matches(
                playback_round,
                frame.header.round_id.as_deref(),
                frame.header.playback_id.as_deref(),
            ) {
                warn!(
                    frame_round_id = frame.header.round_id.as_deref().unwrap_or("-"),
                    frame_playback_id = frame.header.playback_id.as_deref().unwrap_or("-"),
                    current_round_id = %playback_round.round_id,
                    current_playback_id = %playback_round.playback_id,
                    chunk_seq = chunk_seq.unwrap_or(0),
                    bytes = payload_bytes,
                    "[TTS][WARN] 收到过期轮次音频块，已丢弃"
                );
                return state;
            }
        }
        let decoded_samples =
            match tts_opus_decoder.decode_packet_stream(&frame.payload, frame.header.duration_ms) {
                Ok(samples) => samples,
                Err(err) => {
                    warn!(
                        round_id = frame.header.round_id.as_deref().unwrap_or("-"),
                        playback_id = frame.header.playback_id.as_deref().unwrap_or("-"),
                        chunk_seq = chunk_seq.unwrap_or(0),
                        bytes = payload_bytes,
                        "[TTS][WARN] Opus 下行解码失败，音频块已丢弃: {}",
                        err
                    );
                    return state;
                }
            };
        if let Some(playback_round) = current_playback_round.as_mut() {
            if playback_round.first_audio_received_at.is_none() {
                playback_round.first_audio_received_at = Some(Instant::now());
                if let Some(sent_at_ms) = frame.header.timestamp_ms {
                    let now_ms = current_epoch_ms();
                    let gateway_to_client_ms = (now_ms - sent_at_ms as i128).max(0);
                    info!(
                        round_id = %playback_round.round_id,
                        playback_id = %playback_round.playback_id,
                        chunk_seq = chunk_seq.unwrap_or(0),
                        gateway_to_client_ms,
                        "[TTFA] 客户端收到首个 TTS Opus"
                    );
                } else {
                    info!(
                        round_id = %playback_round.round_id,
                        playback_id = %playback_round.playback_id,
                        chunk_seq = chunk_seq.unwrap_or(0),
                        "[TTFA] 客户端收到首个 TTS Opus"
                    );
                }
            }
        }
        let decoded_bytes: Vec<u8> = decoded_samples
            .iter()
            .flat_map(|sample| sample.to_le_bytes())
            .collect();
        if let Err(err) = tts_cmd_tx.try_send(TtsProcessorCommand::Audio(decoded_bytes)) {
            warn!(
                round_id = frame.header.round_id.as_deref().unwrap_or("-"),
                playback_id = frame.header.playback_id.as_deref().unwrap_or("-"),
                chunk_seq = chunk_seq.unwrap_or(0),
                bytes = payload_bytes,
                decoded_samples = decoded_samples.len(),
                "[TTS][WARN] TTS 处理队列提交失败，Opus 音频块已丢弃: {}",
                err
            );
        }
        state
    }

    fn schedule_feedback(
        playback_tx: CrossbeamSender<PlaybackCommand>,
        playback_generation: PlaybackGeneration,
        samples: Option<&[i16]>,
        delay: Duration,
        label: &'static str,
    ) {
        let Some(samples) = samples.filter(|samples| !samples.is_empty()) else {
            return;
        };
        let samples = samples.to_vec();
        let generation = playback_generation.current();
        tokio::spawn(async move {
            tokio::time::sleep(delay).await;
            if !playback_generation.is_current(generation) {
                tracing::debug!("[PLAY] {}已取消: playback generation 已变化", label);
                return;
            }
            if let Err(err) = playback_tx.try_send(PlaybackCommand::PlayFeedbackPcm16k {
                generation,
                samples,
            }) {
                warn!("[PLAY][WARN] {}提交失败: {}", label, err);
            }
        });
    }

    fn spawn_gateway_dispatch(
        &self,
        mut raw_events: mpsc::Receiver<TransportEvent>,
        control_tx: mpsc::Sender<TransportEvent>,
    ) {
        tokio::spawn(async move {
            while let Some(event) = raw_events.recv().await {
                if control_tx.send(event).await.is_err() {
                    break;
                }
            }
        });
    }

    fn load_asr_ding(&self) -> Option<Vec<i16>> {
        let sound_path = self.config.feedback.asr_ding_sound_path.as_deref()?;

        match load_wav_for_playback(Path::new(sound_path), self.config.audio.sample_rate) {
            Ok(samples) if !samples.is_empty() => {
                info!("[PLAY] ASR 确认音已加载: {}", sound_path);
                Some(samples)
            }
            Ok(_) => {
                warn!("[PLAY][WARN] ASR 确认音为空: {}", sound_path);
                None
            }
            Err(err) => {
                warn!("[PLAY][WARN] 加载 ASR 确认音失败: {}", err);
                None
            }
        }
    }

    fn start_capture_for_dialogue(&self, reason: &str) -> Result<CaptureHandle> {
        let started_at = Instant::now();
        let capture = match self.config.audio.frontend_mode {
            AudioFrontendMode::Cpal => start_capture(&self.config.audio)?,
            AudioFrontendMode::Tcp => start_capture_tcp(&self.config.audio)?,
        };
        info!(
            "[AUDIO] 麦克风采集已启动: reason={}, mode={}, elapsed={:.0}ms",
            reason,
            self.config.audio.frontend_mode.as_str(),
            started_at.elapsed().as_secs_f64() * 1000.0
        );
        Ok(capture)
    }

    fn start_playback_for_dialogue(&self) -> Result<PlaybackHandle> {
        match self.config.audio.frontend_mode {
            AudioFrontendMode::Cpal => start_playback(
                &self.config.audio,
                self.config.tts.volume_gain,
                self.config.tts.agc_target_db,
                self.audio_ducking.clone(),
            ),
            AudioFrontendMode::Tcp => start_playback_tcp(
                &self.config.audio,
                self.config.tts.volume_gain,
                self.config.tts.agc_target_db,
                self.audio_ducking.clone(),
            ),
        }
    }

    fn connect_audio_frontend_control(&self, speaking: bool) -> Option<AudioFrontendControl> {
        if self.config.audio.frontend_mode != AudioFrontendMode::Tcp {
            return None;
        }
        match AudioFrontendControl::connect(&self.config.audio) {
            Ok(mut control) => {
                match control.query_status() {
                    Ok(status) => Self::log_audio_frontend_status(&status),
                    Err(err) => warn!("[AUDIO_FRONTEND][WARN] status 查询失败: {}", err),
                }
                let restore_result = control.restore_speaking_state(
                    speaking,
                    self.config.audio.frontend_mute_mic_during_playback,
                );
                if let Err(err) = restore_result {
                    warn!(
                        speaking,
                        "[AUDIO_FRONTEND][WARN] control 状态恢复失败: {}", err
                    );
                    return None;
                }
                Some(control)
            }
            Err(err) => {
                warn!(
                    "[AUDIO_FRONTEND][WARN] control TCP 未连接，继续使用音频 TCP: {}",
                    err
                );
                None
            }
        }
    }

    fn log_audio_frontend_status(status: &AudioFrontendStatus) {
        info!(
            "[AUDIO_FRONTEND] status: backend={}, aec_active={}, high_pass_active={}, ns_active={}, agc_active={}, kws_tap_active={}, webrtc_compiled={}, frames={}, missing_render={}, kws_frames={}, kws_errors={}, processor_errors={}, protocol_errors={}, socket_errors={}",
            status.processor_backend.as_deref().unwrap_or("unknown"),
            Self::optional_bool_label(status.processor_aec_active),
            Self::optional_bool_label(status.processor_high_pass_filter_active),
            Self::optional_bool_label(status.processor_noise_suppression_active),
            Self::optional_bool_label(status.processor_agc_active),
            Self::optional_bool_label(status.processor_kws_audio_tap_active),
            Self::optional_bool_label(status.processor_webrtc_compiled),
            Self::optional_u64_label(status.processor_frames),
            Self::optional_u64_label(status.processor_missing_render),
            Self::optional_u64_label(status.processor_kws_frames),
            Self::optional_u64_label(status.processor_kws_errors),
            Self::optional_u64_label(status.processor_errors),
            Self::optional_u64_label(status.protocol_errors),
            Self::optional_u64_label(status.socket_errors),
        );
    }

    fn optional_bool_label(value: Option<bool>) -> &'static str {
        match value {
            Some(true) => "yes",
            Some(false) => "no",
            None => "unknown",
        }
    }

    fn optional_u64_label(value: Option<u64>) -> String {
        value
            .map(|number| number.to_string())
            .unwrap_or_else(|| "unknown".to_string())
    }

    fn set_audio_frontend_speaking(
        control: &mut Option<AudioFrontendControl>,
        speaking: &mut bool,
        mute_mic_during_playback: bool,
        enabled: bool,
        reason: &str,
    ) {
        let clear_buffered_playback = Self::should_clear_audio_frontend_playback(enabled, reason);
        let state_changed = *speaking != enabled;
        if !state_changed && !clear_buffered_playback {
            return;
        }
        if state_changed {
            *speaking = enabled;
        }

        let Some(active_control) = control.as_mut() else {
            return;
        };

        let result = if enabled {
            if mute_mic_during_playback {
                active_control
                    .set_mic_output_muted(true)
                    .and_then(|_| active_control.speaking_started())
            } else {
                active_control.speaking_started()
            }
        } else {
            let interrupt_result = if clear_buffered_playback {
                active_control.playback_interrupt()
            } else {
                Ok(())
            };
            if !state_changed {
                interrupt_result
            } else if mute_mic_during_playback {
                interrupt_result
                    .and_then(|_| active_control.speaking_finished())
                    .and_then(|_| active_control.set_mic_output_muted(false))
            } else {
                interrupt_result.and_then(|_| active_control.speaking_finished())
            }
        };

        if let Err(err) = result {
            warn!(
                "[AUDIO_FRONTEND][WARN] control 发送失败: reason={}, enabled={}, error={}",
                reason, enabled, err
            );
            *control = None;
        }
    }

    fn should_clear_audio_frontend_playback(enabled: bool, reason: &str) -> bool {
        !enabled && reason != "playback_finished" && reason != "playback_cancelled_after_finish"
    }

    fn ensure_capture_running(
        &self,
        capture: &mut Option<CaptureHandle>,
        reason: &str,
    ) -> Result<()> {
        if capture.is_some() {
            return Ok(());
        }

        *capture = Some(self.start_capture_for_dialogue(reason)?);
        Ok(())
    }

    fn pause_capture_for_waiting(&self, capture: &mut Option<CaptureHandle>) {
        if !self.config.audio.should_pause_capture_while_waiting() {
            return;
        }
        if capture.take().is_some() {
            info!("[AUDIO] 等待唤醒，麦克风采集已暂停");
        }
    }

    async fn start_recording_audio_stream(
        &self,
        conn: &HybridTransport,
        upload_seq: u64,
    ) -> Result<RecordingUpload> {
        self.wait_for_audio_uplink_ready(conn, "start_recording")
            .await?;
        let utterance_id = format!("{}:{upload_seq}", current_epoch_ms());
        let trace_id = format!("audio-{utterance_id}");
        conn.send_control(&ClientMessage::AudioStart {
            bot_id: self.config.gateway.bot_id.clone(),
            trace_id: Some(trace_id.clone()),
            utterance_id: utterance_id.clone(),
            sample_rate: self.config.audio.sample_rate,
            channels: self.config.audio.channels,
            opus_frame_ms: 20,
        })
        .await?;
        let chunk_ms = 100usize;
        let chunk_samples = ((self.config.audio.sample_rate as usize * chunk_ms) / 1000)
            * self.config.audio.channels as usize;
        info!(
            "[AUDIO] 开始流式上送录音: utterance_id={}, trace_id={}, chunk_ms={}ms",
            utterance_id, trace_id, chunk_ms
        );
        Ok(RecordingUpload {
            utterance_id,
            trace_id,
            chunk_seq: 0,
            total_samples: 0,
            total_chunks: 0,
            total_packets: 0,
            total_opus_bytes: 0,
            started_at: Instant::now(),
            chunk_samples: chunk_samples.max(1),
        })
    }

    async fn start_barge_in_audio_stream(
        &self,
        conn: &HybridTransport,
        upload_seq: u64,
        round: &PlaybackRound,
        speech_epoch: u64,
    ) -> Result<RecordingUpload> {
        self.wait_for_audio_uplink_ready(conn, "start_barge_in")
            .await?;
        let utterance_id = format!("barge:{}:{upload_seq}", current_epoch_ms());
        let trace_id = format!("audio-{utterance_id}");
        conn.send_control(&ClientMessage::BargeInStart {
            bot_id: self.config.gateway.bot_id.clone(),
            trace_id: Some(trace_id.clone()),
            utterance_id: utterance_id.clone(),
            round_id: round.round_id.clone(),
            playback_id: round.playback_id.clone(),
            speech_epoch,
            sample_rate: self.config.audio.sample_rate,
            channels: self.config.audio.channels,
            opus_frame_ms: 20,
        })
        .await?;
        let chunk_samples =
            (self.config.audio.sample_rate as usize / 10) * self.config.audio.channels as usize;
        info!(
            utterance_id,
            round_id = %round.round_id,
            playback_id = %round.playback_id,
            speech_epoch,
            "[BARGE_IN] 开始候选音频上送"
        );
        Ok(RecordingUpload {
            utterance_id,
            trace_id,
            chunk_seq: 0,
            total_samples: 0,
            total_chunks: 0,
            total_packets: 0,
            total_opus_bytes: 0,
            started_at: Instant::now(),
            chunk_samples: chunk_samples.max(1),
        })
    }

    async fn cancel_barge_in_audio_stream(
        &self,
        conn: &HybridTransport,
        upload: &mut Option<RecordingUpload>,
        schedule: &mut Option<BargeInProbeSchedule>,
        frames: &mut Vec<Vec<i16>>,
        reason: &str,
    ) {
        let active_upload = upload.take();
        let active_schedule = schedule.take();
        frames.clear();
        let (Some(active_upload), Some(active_schedule)) = (active_upload, active_schedule) else {
            return;
        };
        let identity = active_schedule.identity();
        let _ = conn
            .send_control(&ClientMessage::BargeInCancel {
                bot_id: self.config.gateway.bot_id.clone(),
                trace_id: Some(active_upload.trace_id.clone()),
                utterance_id: identity.utterance_id.clone(),
                round_id: identity.round_id.clone(),
                playback_id: identity.playback_id.clone(),
                candidate_seq: active_schedule.current_candidate_seq(),
                speech_epoch: identity.speech_epoch,
                audio_watermark: active_upload.total_samples as u64,
                reason: reason.to_string(),
            })
            .await;
        info!(
            utterance_id = %identity.utterance_id,
            reason,
            samples = active_upload.total_samples,
            "[BARGE_IN] 候选音频已取消"
        );
    }

    async fn wait_for_audio_uplink_ready(
        &self,
        conn: &HybridTransport,
        reason: &'static str,
    ) -> Result<()> {
        if conn.audio_uplink_ready()
            || self.config.transport.webrtc_audio_uplink_mode != WebRtcAudioUplinkMode::RtpOnly
        {
            return Ok(());
        }

        let started_at = Instant::now();
        let timeout = Duration::from_millis(1_500);
        warn!(
            reason,
            timeout_ms = timeout.as_millis() as u64,
            "[RTC][WARN] RTP 上行尚未 ready，录音启动前等待"
        );
        while started_at.elapsed() < timeout {
            time::sleep(Duration::from_millis(50)).await;
            if conn.audio_uplink_ready() {
                info!(
                    reason,
                    waited_ms = started_at.elapsed().as_millis() as u64,
                    "[RTC] RTP 上行已 ready"
                );
                return Ok(());
            }
        }

        Err(anyhow::anyhow!(
            "RTC_AUDIO_UPLINK=rtp_only 但 WebRTC RTP 上行在 {}ms 内未就绪",
            timeout.as_millis()
        ))
    }

    async fn flush_recording_audio_chunks(
        &self,
        conn: &HybridTransport,
        upload: &mut RecordingUpload,
        recording_frames: &mut Vec<Vec<i16>>,
        force: bool,
    ) -> Result<()> {
        let mut samples: Vec<i16> = recording_frames
            .iter()
            .flat_map(|c| c.iter().copied())
            .collect();
        if samples.is_empty() {
            return Ok(());
        }

        let mut consumed_samples = 0usize;
        while samples.len().saturating_sub(consumed_samples) >= upload.chunk_samples
            || (force && consumed_samples < samples.len())
        {
            let remaining = samples.len() - consumed_samples;
            let take = if force {
                remaining.min(upload.chunk_samples)
            } else {
                upload.chunk_samples
            };
            let end = consumed_samples + take;
            let chunk = &samples[consumed_samples..end];
            let opus = encode_pcm16_opus_packet_stream(
                chunk,
                self.config.audio.sample_rate,
                self.config.audio.channels,
            )?;
            let duration_ms = chunk.len() as f64
                / self.config.audio.sample_rate as f64
                / self.config.audio.channels as f64
                * 1000.0;
            upload.chunk_seq += 1;
            let header = AudioFrameHeader::client_input_opus_chunk(
                self.config.gateway.bot_id.clone(),
                Some(upload.trace_id.clone()),
                upload.utterance_id.clone(),
                upload.chunk_seq,
                self.config.audio.sample_rate,
                self.config.audio.channels,
                duration_ms,
                opus.frame_duration_ms,
                opus.packet_count,
            );
            conn.send_audio_frame(&header, &opus.payload).await?;
            upload.total_samples += chunk.len();
            upload.total_chunks += 1;
            upload.total_packets += opus.packet_count as u64;
            upload.total_opus_bytes += opus.payload.len() as u64;
            tracing::debug!(
                "[AUDIO] 已上送录音分片: utterance_id={}, trace_id={}, chunk_seq={}, samples={}, packets={}, opus_bytes={}",
                upload.utterance_id,
                upload.trace_id,
                upload.chunk_seq,
                chunk.len(),
                opus.packet_count,
                opus.payload.len(),
            );
            consumed_samples = end;
        }

        if consumed_samples > 0 {
            let remaining = samples.split_off(consumed_samples);
            recording_frames.clear();
            if !remaining.is_empty() {
                recording_frames.push(remaining);
            }
        }
        Ok(())
    }

    async fn finish_recording_audio_stream(
        &self,
        conn: &HybridTransport,
        upload: &mut RecordingUpload,
        recording_frames: &mut Vec<Vec<i16>>,
    ) -> Result<usize> {
        self.flush_recording_audio_chunks(conn, upload, recording_frames, true)
            .await?;
        let duration_ms = upload.total_samples as f64
            / self.config.audio.sample_rate as f64
            / self.config.audio.channels as f64
            * 1000.0;
        conn.send_control(&ClientMessage::AudioEnd {
            bot_id: self.config.gateway.bot_id.clone(),
            trace_id: Some(upload.trace_id.clone()),
            utterance_id: upload.utterance_id.clone(),
            duration_ms,
            samples: upload.total_samples as u64,
            chunks: upload.total_chunks,
            packets: upload.total_packets,
            pcm_bytes: (upload.total_samples * std::mem::size_of::<i16>()) as u64,
            opus_bytes: upload.total_opus_bytes,
        })
        .await?;
        info!(
            "[AUDIO] 录音已流式上送完成: utterance_id={}, trace_id={}, samples={}, chunks={}, packets={}, pcm_bytes={}, opus_bytes={}, duration={:.0}ms, elapsed={:.0}ms",
            upload.utterance_id,
            upload.trace_id,
            upload.total_samples,
            upload.total_chunks,
            upload.total_packets,
            upload.total_samples * std::mem::size_of::<i16>(),
            upload.total_opus_bytes,
            duration_ms,
            upload.started_at.elapsed().as_secs_f64() * 1000.0,
        );
        Ok(upload.total_samples)
    }

    async fn cancel_recording_audio_stream(
        &self,
        conn: &HybridTransport,
        upload: &mut Option<RecordingUpload>,
        recording_frames: &mut Vec<Vec<i16>>,
        reason: &str,
    ) {
        let Some(active_upload) = upload.take() else {
            recording_frames.clear();
            return;
        };
        let _ = conn
            .send_control(&ClientMessage::AudioCancel {
                bot_id: self.config.gateway.bot_id.clone(),
                trace_id: Some(active_upload.trace_id.clone()),
                utterance_id: active_upload.utterance_id.clone(),
                reason: reason.to_string(),
            })
            .await;
        recording_frames.clear();
        info!(
            "[AUDIO] 已取消流式录音上送: utterance_id={}, trace_id={}, reason={}, chunks={}, samples={}",
            active_upload.utterance_id,
            active_upload.trace_id,
            reason,
            active_upload.total_chunks,
            active_upload.total_samples,
        );
    }

    async fn send_client_event(
        &self,
        conn: &HybridTransport,
        event: &str,
        source: &str,
    ) -> Result<String> {
        let event_id = Self::new_client_event_id(event);
        conn.send_control(&ClientMessage::ClientEvent {
            event: event.to_string(),
            bot_id: self.config.gateway.bot_id.clone(),
            source: source.to_string(),
            trace_id: Some(event_id.clone()),
            event_id: Some(event_id.clone()),
        })
        .await?;
        Ok(event_id)
    }

    fn new_client_event_id(event: &str) -> String {
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_millis())
            .unwrap_or_default();
        format!("{event}-{millis}")
    }

    async fn send_environment_text(&self, conn: &HybridTransport, content: &str) -> Result<()> {
        // content 由 HTTP 调用方提供，语义是「环境感知系统观察到的环境描述」。
        // LLM 收到后会根据描述主动生成回应，而不是把 content 误当成「用户说的话」。
        let text = format!("请根据以下环境主动回应：{content}");
        conn.send_control(&ClientMessage::Text {
            content: text,
            bot_id: self.config.gateway.bot_id.clone(),
            source: "environment_http".to_string(),
            trace_id: Some(Self::new_client_event_id("environment_text")),
        })
        .await?;
        Ok(())
    }

    async fn send_playback_report(
        &self,
        conn: &HybridTransport,
        playback_round: PlaybackRound,
        stats: PlaybackStats,
        interrupted_reason: Option<String>,
    ) {
        let max_buffered_samples = stats.max_buffered_samples as u64;
        let report_at = Instant::now();
        let first_audio_to_playback_start_ms = duration_ms_between(
            playback_round.first_audio_received_at,
            playback_round.playback_started_at,
        );
        let playback_start_to_complete_ms =
            duration_ms_between(playback_round.playback_started_at, Some(report_at));
        let message = if let Some(reason) = interrupted_reason {
            ClientMessage::PlaybackInterrupted {
                trace_id: Some(playback_round.round_id.clone()),
                round_id: playback_round.round_id.clone(),
                playback_id: playback_round.playback_id.clone(),
                reason: Some(reason),
                first_audio_to_playback_start_ms,
                playback_start_to_complete_ms,
                pushed_chunks: stats.pushed_chunks,
                pushed_samples: stats.pushed_samples,
                underrun_callbacks: stats.underrun_callbacks,
                zero_filled_samples: stats.zero_filled_samples,
                max_buffered_samples,
            }
        } else {
            ClientMessage::PlaybackComplete {
                trace_id: Some(playback_round.round_id.clone()),
                round_id: playback_round.round_id.clone(),
                playback_id: playback_round.playback_id.clone(),
                first_audio_to_playback_start_ms,
                playback_start_to_complete_ms,
                pushed_chunks: stats.pushed_chunks,
                pushed_samples: stats.pushed_samples,
                underrun_callbacks: stats.underrun_callbacks,
                zero_filled_samples: stats.zero_filled_samples,
                max_buffered_samples,
            }
        };

        if let Err(err) = conn.send_control(&message).await {
            warn!(
                round_id = %playback_round.round_id,
                playback_id = %playback_round.playback_id,
                "[PLAY][WARN] 本地播放完成回执发送失败: {}", err
            );
        }
    }

    fn notify_perception_start(&self, reason: &'static str) {
        let Some(url) = self.config.environment_trigger.perception_start_url.clone() else {
            return;
        };

        tokio::task::spawn_blocking(move || {
            let client = match reqwest::blocking::Client::builder()
                .timeout(Duration::from_secs(3))
                .build()
            {
                Ok(client) => client,
                Err(err) => {
                    warn!(
                        error = %err,
                        reason = reason,
                        "[ENV][WARN] 环境感知 HTTP 客户端初始化失败"
                    );
                    return;
                }
            };
            let result = client.get(&url).send();
            match result {
                Ok(response) => {
                    info!(
                        status = %response.status(),
                        reason = reason,
                        "[ENV] 已通知环境感知启动"
                    );
                }
                Err(err) => {
                    warn!(
                        error = %err,
                        reason = reason,
                        "[ENV][WARN] 通知环境感知启动失败"
                    );
                }
            }
        });
    }

    fn drain_capture_frames(&self, capture: Option<&CaptureHandle>) {
        let Some(capture) = capture else {
            return;
        };

        while capture.frames.try_recv().is_ok() {}
    }

    fn clear_wake_audio_buffers(
        wake_capture_prebuffer: &mut VecDeque<Vec<i16>>,
        recording_preroll: &mut VecDeque<Vec<i16>>,
        collect_wake_capture_prebuffer: &mut bool,
    ) {
        wake_capture_prebuffer.clear();
        recording_preroll.clear();
        *collect_wake_capture_prebuffer = false;
    }

    fn should_drain_capture_after_wake(frontend_mode: AudioFrontendMode) -> bool {
        frontend_mode != AudioFrontendMode::Tcp
    }

    fn should_preserve_wake_prebuffer_during_playback(
        frontend_mode: AudioFrontendMode,
        mute_mic_during_playback: bool,
        collect_wake_capture_prebuffer: bool,
    ) -> bool {
        collect_wake_capture_prebuffer
            && frontend_mode == AudioFrontendMode::Tcp
            && !mute_mic_during_playback
    }

    fn should_clear_wake_prebuffer_on_playback_started(
        frontend_mode: AudioFrontendMode,
        mute_mic_during_playback: bool,
        collect_wake_capture_prebuffer: bool,
    ) -> bool {
        !Self::should_preserve_wake_prebuffer_during_playback(
            frontend_mode,
            mute_mic_during_playback,
            collect_wake_capture_prebuffer,
        )
    }

    fn push_audio_prebuffer(prebuffer: &mut VecDeque<Vec<i16>>, capacity: usize, frame: Vec<i16>) {
        if capacity == 0 {
            return;
        }
        if prebuffer.len() == capacity {
            prebuffer.pop_front();
        }
        prebuffer.push_back(frame);
    }

    fn drain_wake_capture_prebuffer(
        prebuffer: &mut VecDeque<Vec<i16>>,
        trigger_window: &mut VadTriggerWindow,
        recording_preroll: &mut VecDeque<Vec<i16>>,
        recording_preroll_capacity: usize,
        vad: &mut VadDetector,
        speech_start_min_dbfs: f32,
    ) -> usize {
        let mut speech_frames = 0;
        while let Some(frame) = prebuffer.pop_front() {
            let is_trigger_speech = vad
                .is_start_trigger_speech(&frame, speech_start_min_dbfs)
                .unwrap_or(false);
            if is_trigger_speech {
                speech_frames += 1;
            }
            Self::push_audio_prebuffer(
                recording_preroll,
                recording_preroll_capacity,
                frame.clone(),
            );
            trigger_window.push(frame, is_trigger_speech);
        }
        speech_frames
    }

    fn format_wake_event(&self, event: &SerialWakeEvent) -> String {
        format!(
            "port={}, keyword={:?}, angle={:?}, beamforming={:?}, raw={:?}",
            event.port,
            event.fields.keyword,
            event.fields.angle,
            event.fields.beamforming,
            event.raw,
        )
    }

    fn try_recv_wake_event(
        wake_monitor: Option<&SerialWakeMonitor>,
        http_wake_monitor: Option<&HttpWakeMonitor>,
        kws_wake_monitor: Option<&KwsWakeMonitor>,
    ) -> Option<SerialWakeEvent> {
        if let Some(wake_monitor) = wake_monitor {
            if let Ok(event) = wake_monitor.try_recv() {
                return Some(event);
            }
        }
        if let Some(http_wake_monitor) = http_wake_monitor {
            if let Ok(event) = http_wake_monitor.try_recv() {
                return Some(event);
            }
        }
        kws_wake_monitor.and_then(|monitor| monitor.try_recv().ok())
    }

    fn accept_wake_event(
        event: &SerialWakeEvent,
        debounce: Duration,
        last_accepted_at: &mut Option<Instant>,
    ) -> bool {
        if let Some(last) = *last_accepted_at {
            if event.received_at <= last || event.received_at.duration_since(last) < debounce {
                return false;
            }
        }
        *last_accepted_at = Some(event.received_at);
        true
    }

    fn reset_recording_state(
        trigger_window: &mut VadTriggerWindow,
        recording_frames: &mut Vec<Vec<i16>>,
        silence_started_at: &mut Option<Instant>,
    ) {
        trigger_window.clear();
        recording_frames.clear();
        *silence_started_at = None;
    }

    fn wake_event_for_state(state: &DialogueState) -> &'static str {
        match state {
            DialogueState::SendingAudio { .. } | DialogueState::TtsPlaying => "wake_interrupt",
            DialogueState::WaitingForWakeWord
            | DialogueState::AwakeIdle { .. }
            | DialogueState::Recording { .. } => "wake_idle",
        }
    }

    fn should_suppress_stale_server_messages_on_wake(wake_event: &str) -> bool {
        wake_event == "wake_interrupt"
    }

    fn state_after_playback_cancel(state: DialogueState) -> DialogueState {
        match state {
            DialogueState::SendingAudio { .. } | DialogueState::TtsPlaying => {
                Self::awake_idle_state()
            }
            other => other,
        }
    }

    fn awake_idle_state() -> DialogueState {
        DialogueState::AwakeIdle {
            last_interaction: Instant::now(),
        }
    }

    fn state_after_done_without_playback(
        state: DialogueState,
        pending_playback_state: &mut Option<DialogueState>,
        pending_exit: &mut Option<bool>,
        current_playback_round: &mut Option<PlaybackRound>,
        exit: bool,
    ) -> (DialogueState, bool) {
        if !matches!(state, DialogueState::SendingAudio { .. }) {
            return (state, false);
        }
        if current_playback_round
            .as_ref()
            .is_some_and(|round| round.first_audio_received_at.is_some())
        {
            return (state, false);
        }

        pending_exit.take();
        current_playback_round.take();
        if let Some(next_state) = pending_playback_state.take() {
            return (next_state, true);
        }
        if exit {
            return (DialogueState::WaitingForWakeWord, true);
        }
        (Self::awake_idle_state(), true)
    }

    fn done_without_playback_exit_candidate(
        message: &ServerMessage,
        state: &DialogueState,
        current_playback_round: Option<&PlaybackRound>,
        stale_playback_rounds: &[PlaybackRoundKey],
        stale_server_trace_ids: &[String],
    ) -> Option<bool> {
        let ServerMessage::Done {
            exit,
            trace_id,
            round_id,
            playback_id,
        } = message
        else {
            return None;
        };
        if !state.accepts_server_round_messages() {
            return None;
        }
        if !Self::done_matches_current_playback_round(
            current_playback_round,
            round_id.as_deref(),
            playback_id.as_deref(),
        ) {
            return None;
        }
        if Self::stale_playback_round_matches(
            stale_playback_rounds,
            round_id.as_deref(),
            playback_id.as_deref(),
        ) {
            return None;
        }
        if Self::stale_server_trace_matches(stale_server_trace_ids, trace_id.as_deref()) {
            return None;
        }
        Some(*exit)
    }

    fn done_matches_current_playback_round(
        current_playback_round: Option<&PlaybackRound>,
        round_id: Option<&str>,
        playback_id: Option<&str>,
    ) -> bool {
        current_playback_round
            .map(|round| Self::playback_round_matches(round, round_id, playback_id))
            .unwrap_or(true)
    }

    fn state_after_response_timeout(
        pending_playback_state: &mut Option<DialogueState>,
    ) -> DialogueState {
        pending_playback_state
            .take()
            .unwrap_or_else(Self::awake_idle_state)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        App, PendingPrePlaybackRtp, PendingTurnCommit, PlaybackEventDisposition, PlaybackRound,
        PlaybackRoundKey, StalePlaybackFilter, PRE_PLAYBACK_RTP_MAX_FRAMES,
    };
    use crate::audio::playback::{PlaybackCommand, PlaybackGeneration};
    use crate::config::AudioFrontendMode;
    use crate::protocol::{AudioFrame, AudioFrameHeader, ServerMessage, AUDIO_FRAME_VERSION};
    use crate::state::DialogueState;
    use crate::wake::serial_monitor::{SerialWakeEvent, WakeFields};
    use crossbeam::channel::bounded;
    use std::collections::VecDeque;
    use std::time::{Duration, Instant};

    fn playback_round() -> PlaybackRound {
        PlaybackRound {
            round_id: "round-new".to_string(),
            playback_id: "playback-new".to_string(),
            generation: 0,
            rtp_start_sequence: None,
            first_audio_received_at: None,
            playback_started_at: Some(Instant::now()),
        }
    }

    #[tokio::test]
    async fn schedules_feedback_after_delay() {
        let (tx, rx) = bounded(1);
        let generation = PlaybackGeneration::default();
        App::schedule_feedback(
            tx,
            generation,
            Some(&[100, -100]),
            Duration::from_millis(5),
            "test",
        );

        tokio::time::sleep(Duration::from_millis(20)).await;
        match rx.try_recv().expect("scheduled feedback") {
            PlaybackCommand::PlayFeedbackPcm16k {
                generation,
                samples,
            } => {
                assert_eq!(generation, 0);
                assert_eq!(samples, vec![100, -100]);
            }
            other => panic!("unexpected playback command: {other:?}"),
        }
    }

    #[tokio::test]
    async fn cancels_scheduled_feedback_after_generation_change() {
        let (tx, rx) = bounded(1);
        let generation = PlaybackGeneration::default();
        App::schedule_feedback(
            tx,
            generation.clone(),
            Some(&[100, -100]),
            Duration::from_millis(10),
            "test",
        );
        generation.advance();

        tokio::time::sleep(Duration::from_millis(25)).await;
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn cancel_after_final_render_classifies_old_finished_as_cancelled_terminal() {
        let generation = PlaybackGeneration::default();
        let old_generation = generation.current();
        let mut old_round = playback_round();
        old_round.generation = old_generation;
        generation.advance();
        let mut new_round = playback_round();
        new_round.generation = generation.current();

        assert_eq!(
            App::classify_playback_event(Some(&old_round), old_generation, generation.current(),),
            PlaybackEventDisposition::Cancelled,
        );
        assert_eq!(
            App::classify_playback_event(Some(&new_round), old_generation, generation.current(),),
            PlaybackEventDisposition::Stale,
        );
        assert_eq!(
            App::classify_playback_event(
                Some(&new_round),
                generation.current(),
                generation.current(),
            ),
            PlaybackEventDisposition::Current,
        );
    }

    fn server_tts_frame(
        trace_id: Option<&str>,
        round_id: Option<&str>,
        playback_id: Option<&str>,
    ) -> AudioFrame {
        AudioFrame {
            header: AudioFrameHeader {
                kind: "audio_frame".to_string(),
                version: AUDIO_FRAME_VERSION,
                encoding: "opus".to_string(),
                direction: Some("server_tts".to_string()),
                bot_id: None,
                trace_id: trace_id.map(str::to_string),
                round_id: round_id.map(str::to_string),
                playback_id: playback_id.map(str::to_string),
                utterance_id: None,
                seq: Some(1),
                chunk_seq: Some(1),
                timestamp_ms: None,
                duration_ms: Some(20.0),
                sample_rate: Some(16_000),
                channels: Some(1),
                opus_frame_ms: Some(20),
                packet_count: Some(1),
                stream_event: Some("chunk".to_string()),
            },
            payload: vec![0xf8, 0xff, 0xfe],
        }
    }

    #[test]
    fn playback_round_matcher_rejects_stale_round_messages() {
        let round = playback_round();

        assert!(App::playback_round_matches(
            &round,
            Some("round-new"),
            Some("playback-new"),
        ));
        assert!(App::playback_round_matches(&round, Some("round-new"), None));
        assert!(!App::playback_round_matches(
            &round,
            Some("round-old"),
            Some("playback-new"),
        ));
        assert!(!App::playback_round_matches(
            &round,
            Some("round-new"),
            Some("playback-old"),
        ));
    }

    #[test]
    fn stale_audio_frame_matcher_rejects_current_round_rtp_without_ids() {
        let round = playback_round();
        let stale = vec![PlaybackRoundKey::from(&round)];
        let frame = AudioFrame {
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
                seq: Some(1),
                chunk_seq: Some(1),
                timestamp_ms: None,
                duration_ms: Some(20.0),
                sample_rate: Some(16_000),
                channels: Some(1),
                opus_frame_ms: Some(20),
                packet_count: Some(1),
                stream_event: Some("chunk".to_string()),
            },
            payload: vec![0xf8, 0xff, 0xfe],
        };

        assert!(App::stale_audio_frame_matches(
            &frame,
            Some(&round),
            StalePlaybackFilter {
                rounds: &stale,
                trace_ids: &[],
            },
        ));
        assert!(!App::stale_audio_frame_matches(
            &frame,
            None,
            StalePlaybackFilter {
                rounds: &stale,
                trace_ids: &[],
            },
        ));
    }

    #[test]
    fn stale_audio_frame_matcher_rejects_rtp_before_round_sequence_boundary() {
        let mut round = playback_round();
        round.rtp_start_sequence = Some(100);
        let mut frame = server_tts_frame(None, None, None);
        frame.header.seq = Some(99);
        frame.header.chunk_seq = Some(99);

        assert!(App::stale_audio_frame_matches(
            &frame,
            Some(&round),
            StalePlaybackFilter {
                rounds: &[],
                trace_ids: &[],
            },
        ));

        frame.header.seq = Some(100);
        frame.header.chunk_seq = Some(100);
        assert!(!App::stale_audio_frame_matches(
            &frame,
            Some(&round),
            StalePlaybackFilter {
                rounds: &[],
                trace_ids: &[],
            },
        ));
    }

    #[test]
    fn rtp_sequence_boundary_handles_wraparound() {
        assert!(App::rtp_sequence_precedes(u16::MAX, 0));
        assert!(!App::rtp_sequence_precedes(0, u16::MAX));
    }

    #[test]
    fn pre_playback_rtp_is_buffered_only_while_waiting_for_playback_start() {
        let frame = server_tts_frame(None, None, None);
        let waiting = DialogueState::SendingAudio {
            last_interaction: Instant::now(),
            waiting_started_at: Instant::now(),
        };

        assert!(App::should_buffer_pre_playback_rtp(&frame, &waiting, None));
        assert!(!App::should_buffer_pre_playback_rtp(
            &frame,
            &DialogueState::TtsPlaying,
            None,
        ));
        assert!(!App::should_buffer_pre_playback_rtp(
            &frame,
            &waiting,
            Some(&playback_round()),
        ));
    }

    #[test]
    fn pre_playback_rtp_reorders_from_round_boundary_and_drops_stale_frames() {
        let now = Instant::now();
        let mut pending = VecDeque::new();
        for sequence in [101u64, 99, 100] {
            let mut frame = server_tts_frame(None, None, None);
            frame.header.seq = Some(sequence);
            frame.header.chunk_seq = Some(sequence);
            App::buffer_pre_playback_rtp(&mut pending, frame, 7, now);
        }
        pending.push_back(PendingPrePlaybackRtp {
            received_at: now - Duration::from_millis(600),
            generation: 7,
            frame: server_tts_frame(None, None, None),
        });
        let mut previous_generation = server_tts_frame(None, None, None);
        previous_generation.header.seq = Some(102);
        previous_generation.header.chunk_seq = Some(102);
        pending.push_back(PendingPrePlaybackRtp {
            received_at: now,
            generation: 6,
            frame: previous_generation,
        });

        let mut round = playback_round();
        round.generation = 7;
        round.rtp_start_sequence = Some(100);
        let (frames, dropped) =
            App::take_pre_playback_rtp_for_round(&mut pending, Some(&round), now);

        assert_eq!(dropped, 3);
        assert_eq!(
            frames
                .iter()
                .map(|frame| frame.header.seq.expect("sequence"))
                .collect::<Vec<_>>(),
            vec![100, 101]
        );
        assert!(pending.is_empty());
    }

    #[test]
    fn pre_playback_rtp_buffer_is_bounded() {
        let now = Instant::now();
        let mut pending = VecDeque::new();
        for sequence in 0..40u64 {
            let mut frame = server_tts_frame(None, None, None);
            frame.header.seq = Some(sequence);
            App::buffer_pre_playback_rtp(&mut pending, frame, 1, now);
        }

        assert_eq!(pending.len(), PRE_PLAYBACK_RTP_MAX_FRAMES);
        assert_eq!(
            pending.front().and_then(|item| item.frame.header.seq),
            Some(8)
        );
        assert_eq!(
            pending.back().and_then(|item| item.frame.header.seq),
            Some(39)
        );
    }

    #[test]
    fn stale_suppression_keeps_ignoring_stale_rtp_without_ids() {
        let round = playback_round();
        let stale = vec![PlaybackRoundKey::from(&round)];
        let frame = server_tts_frame(None, None, None);

        assert!(!App::audio_frame_can_resume_after_stale_suppression(
            &frame,
            Some(&round),
            StalePlaybackFilter {
                rounds: &stale,
                trace_ids: &[],
            },
        ));
    }

    #[test]
    fn stale_suppression_can_resume_for_new_round_audio() {
        let round = playback_round();
        let stale = vec![PlaybackRoundKey {
            round_id: "round-old".to_string(),
            playback_id: "playback-old".to_string(),
        }];
        let frame = server_tts_frame(None, Some("round-new"), Some("playback-new"));

        assert!(App::audio_frame_can_resume_after_stale_suppression(
            &frame,
            Some(&round),
            StalePlaybackFilter {
                rounds: &stale,
                trace_ids: &[],
            },
        ));
    }

    #[test]
    fn stale_audio_frame_matcher_accepts_stale_trace() {
        let mut frame = AudioFrame {
            header: AudioFrameHeader {
                kind: "audio_frame".to_string(),
                version: AUDIO_FRAME_VERSION,
                encoding: "opus".to_string(),
                direction: Some("server_tts".to_string()),
                bot_id: None,
                trace_id: Some("wake_idle-1".to_string()),
                round_id: Some("round-late".to_string()),
                playback_id: Some("round-late:playback".to_string()),
                utterance_id: None,
                seq: Some(1),
                chunk_seq: Some(1),
                timestamp_ms: None,
                duration_ms: Some(20.0),
                sample_rate: Some(16_000),
                channels: Some(1),
                opus_frame_ms: Some(20),
                packet_count: Some(1),
                stream_event: Some("chunk".to_string()),
            },
            payload: vec![0xf8, 0xff, 0xfe],
        };

        assert!(App::stale_audio_frame_matches(
            &frame,
            None,
            StalePlaybackFilter {
                rounds: &[],
                trace_ids: &["wake_idle-1".to_string()],
            },
        ));
        frame.header.trace_id = Some("wake_idle-2".to_string());
        assert!(!App::stale_audio_frame_matches(
            &frame,
            None,
            StalePlaybackFilter {
                rounds: &[],
                trace_ids: &["wake_idle-1".to_string()],
            },
        ));
    }

    #[test]
    fn stale_playback_round_matcher_accepts_round_only_done() {
        let round = playback_round();
        let stale = vec![PlaybackRoundKey::from(&round)];

        assert!(App::stale_playback_round_matches(
            &stale,
            Some("round-new"),
            None
        ));
        assert!(!App::stale_playback_round_matches(
            &stale,
            Some("round-new"),
            Some("other-playback")
        ));
    }

    #[test]
    fn stale_server_trace_tracking_filters_empty_and_duplicates() {
        let mut stale = Vec::new();

        App::remember_stale_server_trace(None, &mut stale, "test");
        App::remember_stale_server_trace(Some(String::new()), &mut stale, "test");
        App::remember_stale_server_trace(Some("trace-1".to_string()), &mut stale, "test");
        App::remember_stale_server_trace(Some("trace-1".to_string()), &mut stale, "test");

        assert_eq!(stale, vec!["trace-1".to_string()]);
        assert!(App::stale_server_trace_matches(&stale, Some("trace-1")));
        assert!(!App::stale_server_trace_matches(&stale, Some("trace-2")));
    }

    #[test]
    fn clear_wake_audio_buffers_resets_preroll_and_collection_flag() {
        let mut wake_prebuffer = VecDeque::from([vec![1i16, 2]]);
        let mut recording_preroll = VecDeque::from([vec![3i16, 4]]);
        let mut collect = true;

        App::clear_wake_audio_buffers(&mut wake_prebuffer, &mut recording_preroll, &mut collect);

        assert!(wake_prebuffer.is_empty());
        assert!(recording_preroll.is_empty());
        assert!(!collect);
    }

    #[test]
    fn wake_event_for_state_accepts_wake_during_recording() {
        let now = Instant::now();

        assert_eq!(
            "wake_idle",
            App::wake_event_for_state(&DialogueState::WaitingForWakeWord)
        );
        assert_eq!(
            "wake_idle",
            App::wake_event_for_state(&DialogueState::AwakeIdle {
                last_interaction: now
            })
        );
        assert_eq!(
            "wake_idle",
            App::wake_event_for_state(&DialogueState::Recording {
                started_at: now,
                last_interaction: now,
            })
        );
        assert_eq!(
            "wake_interrupt",
            App::wake_event_for_state(&DialogueState::SendingAudio {
                last_interaction: now,
                waiting_started_at: now,
            })
        );
        assert_eq!(
            "wake_interrupt",
            App::wake_event_for_state(&DialogueState::TtsPlaying)
        );
    }

    #[test]
    fn wake_idle_does_not_suppress_its_own_server_error() {
        assert!(!App::should_suppress_stale_server_messages_on_wake(
            "wake_idle"
        ));
        assert!(App::should_suppress_stale_server_messages_on_wake(
            "wake_interrupt"
        ));
    }

    #[test]
    fn playback_cancel_releases_pending_wake_response() {
        let now = Instant::now();
        let state = App::state_after_playback_cancel(DialogueState::SendingAudio {
            last_interaction: now,
            waiting_started_at: now,
        });

        assert!(matches!(state, DialogueState::AwakeIdle { .. }));
        assert!(matches!(
            App::state_after_playback_cancel(DialogueState::TtsPlaying),
            DialogueState::AwakeIdle { .. }
        ));
    }

    #[test]
    fn tcp_wake_keeps_capture_queue_for_preroll() {
        assert!(!App::should_drain_capture_after_wake(
            AudioFrontendMode::Tcp
        ));
        assert!(App::should_drain_capture_after_wake(
            AudioFrontendMode::Cpal
        ));
    }

    #[test]
    fn playback_started_preserves_active_wake_prebuffer() {
        assert!(!App::should_clear_wake_prebuffer_on_playback_started(
            AudioFrontendMode::Tcp,
            false,
            true
        ));
        assert!(App::should_clear_wake_prebuffer_on_playback_started(
            AudioFrontendMode::Cpal,
            false,
            true
        ));
        assert!(App::should_clear_wake_prebuffer_on_playback_started(
            AudioFrontendMode::Tcp,
            true,
            true
        ));
        assert!(App::should_clear_wake_prebuffer_on_playback_started(
            AudioFrontendMode::Tcp,
            false,
            false
        ));
    }

    #[test]
    fn global_wake_debounce_filters_any_source() {
        let now = Instant::now();
        let debounce = Duration::from_millis(500);
        let mut last = None;
        let hardware = SerialWakeEvent {
            port: "hardware".to_string(),
            raw: "hardware".to_string(),
            fields: WakeFields {
                keyword: Some("hardware".to_string()),
                angle: None,
                beamforming: None,
            },
            received_at: now,
        };
        let kws_duplicate = SerialWakeEvent {
            port: "kws_tcp".to_string(),
            raw: "kws".to_string(),
            fields: WakeFields {
                keyword: Some("你好康康".to_string()),
                angle: None,
                beamforming: None,
            },
            received_at: now + Duration::from_millis(300),
        };
        let later_kws = SerialWakeEvent {
            received_at: now + Duration::from_millis(700),
            ..kws_duplicate.clone()
        };

        assert!(App::accept_wake_event(&hardware, debounce, &mut last));
        assert!(!App::accept_wake_event(&kws_duplicate, debounce, &mut last));
        assert!(App::accept_wake_event(&later_kws, debounce, &mut last));
    }

    #[test]
    fn done_without_playback_honors_pending_waiting_for_wake_word() {
        let now = Instant::now();
        let mut pending_state = Some(DialogueState::WaitingForWakeWord);
        let mut pending_exit = Some(false);
        let mut current_round = Some(playback_round());

        let (state, applied) = App::state_after_done_without_playback(
            DialogueState::SendingAudio {
                last_interaction: now,
                waiting_started_at: now,
            },
            &mut pending_state,
            &mut pending_exit,
            &mut current_round,
            false,
        );

        assert!(applied);
        assert!(matches!(state, DialogueState::WaitingForWakeWord));
        assert!(pending_state.is_none());
        assert!(pending_exit.is_none());
        assert!(current_round.is_none());
    }

    #[test]
    fn done_without_playback_candidate_ignores_stale_done() {
        let now = Instant::now();
        let stale = vec![PlaybackRoundKey {
            round_id: "round-old".to_string(),
            playback_id: "playback-old".to_string(),
        }];
        let message = ServerMessage::Done {
            exit: false,
            trace_id: None,
            round_id: Some("round-old".to_string()),
            playback_id: Some("playback-old".to_string()),
        };

        let candidate = App::done_without_playback_exit_candidate(
            &message,
            &DialogueState::SendingAudio {
                last_interaction: now,
                waiting_started_at: now,
            },
            None,
            &stale,
            &[],
        );

        assert_eq!(None, candidate);
    }

    #[test]
    fn done_without_playback_candidate_ignores_stale_trace() {
        let now = Instant::now();
        let message = ServerMessage::Done {
            exit: true,
            trace_id: Some("wake_idle-1".to_string()),
            round_id: Some("round-late".to_string()),
            playback_id: Some("round-late:playback".to_string()),
        };

        let candidate = App::done_without_playback_exit_candidate(
            &message,
            &DialogueState::SendingAudio {
                last_interaction: now,
                waiting_started_at: now,
            },
            None,
            &[],
            &["wake_idle-1".to_string()],
        );

        assert_eq!(None, candidate);
    }

    #[test]
    fn done_without_playback_candidate_accepts_current_done() {
        let now = Instant::now();
        let message = ServerMessage::Done {
            exit: true,
            trace_id: None,
            round_id: Some("round-current".to_string()),
            playback_id: Some("playback-current".to_string()),
        };

        let candidate = App::done_without_playback_exit_candidate(
            &message,
            &DialogueState::SendingAudio {
                last_interaction: now,
                waiting_started_at: now,
            },
            None,
            &[],
            &[],
        );

        assert_eq!(Some(true), candidate);
    }

    #[test]
    fn done_without_playback_candidate_ignores_mismatched_current_round() {
        let now = Instant::now();
        let current_round = PlaybackRound {
            round_id: "round-current".to_string(),
            playback_id: "playback-current".to_string(),
            generation: 0,
            rtp_start_sequence: None,
            first_audio_received_at: None,
            playback_started_at: None,
        };
        let message = ServerMessage::Done {
            exit: false,
            trace_id: None,
            round_id: Some("round-old".to_string()),
            playback_id: Some("playback-old".to_string()),
        };

        let candidate = App::done_without_playback_exit_candidate(
            &message,
            &DialogueState::SendingAudio {
                last_interaction: now,
                waiting_started_at: now,
            },
            Some(&current_round),
            &[],
            &[],
        );

        assert_eq!(None, candidate);
    }

    #[test]
    fn done_without_playback_returns_awake_idle_when_no_pending_state() {
        let now = Instant::now();
        let mut pending_state = None;
        let mut pending_exit = Some(false);
        let mut current_round = None;

        let (state, applied) = App::state_after_done_without_playback(
            DialogueState::SendingAudio {
                last_interaction: now,
                waiting_started_at: now,
            },
            &mut pending_state,
            &mut pending_exit,
            &mut current_round,
            false,
        );

        assert!(applied);
        assert!(matches!(state, DialogueState::AwakeIdle { .. }));
        assert!(pending_exit.is_none());
    }

    #[test]
    fn done_without_playback_exit_returns_waiting_for_wake_word() {
        let now = Instant::now();
        let mut pending_state = None;
        let mut pending_exit = Some(true);
        let mut current_round = None;

        let (state, applied) = App::state_after_done_without_playback(
            DialogueState::SendingAudio {
                last_interaction: now,
                waiting_started_at: now,
            },
            &mut pending_state,
            &mut pending_exit,
            &mut current_round,
            true,
        );

        assert!(applied);
        assert!(matches!(state, DialogueState::WaitingForWakeWord));
        assert!(pending_exit.is_none());
    }

    #[test]
    fn done_with_received_audio_waits_for_playback_finish() {
        let now = Instant::now();
        let mut pending_state = Some(DialogueState::WaitingForWakeWord);
        let mut pending_exit = Some(false);
        let mut round = playback_round();
        round.first_audio_received_at = Some(now);
        let mut current_round = Some(round);

        let (state, applied) = App::state_after_done_without_playback(
            DialogueState::SendingAudio {
                last_interaction: now,
                waiting_started_at: now,
            },
            &mut pending_state,
            &mut pending_exit,
            &mut current_round,
            false,
        );

        assert!(!applied);
        assert!(matches!(state, DialogueState::SendingAudio { .. }));
        assert!(pending_state.is_some());
        assert_eq!(Some(false), pending_exit);
        assert!(current_round.is_some());
    }

    #[test]
    fn response_timeout_honors_pending_waiting_for_wake_word() {
        let mut pending_state = Some(DialogueState::WaitingForWakeWord);

        let state = App::state_after_response_timeout(&mut pending_state);

        assert!(matches!(state, DialogueState::WaitingForWakeWord));
        assert!(pending_state.is_none());
    }

    #[test]
    fn response_timeout_returns_awake_idle_without_pending_state() {
        let mut pending_state = None;

        let state = App::state_after_response_timeout(&mut pending_state);

        assert!(matches!(state, DialogueState::AwakeIdle { .. }));
    }

    #[test]
    fn frontend_playback_clear_is_reserved_for_interruptions() {
        assert!(App::should_clear_audio_frontend_playback(
            false,
            "tts_barge_in"
        ));
        assert!(App::should_clear_audio_frontend_playback(
            false,
            "wake_interrupt"
        ));
        assert!(App::should_clear_audio_frontend_playback(
            false,
            "process_shutdown"
        ));
        assert!(!App::should_clear_audio_frontend_playback(
            false,
            "playback_finished"
        ));
        assert!(!App::should_clear_audio_frontend_playback(
            false,
            "playback_cancelled_after_finish"
        ));
        assert!(!App::should_clear_audio_frontend_playback(
            true,
            "tts_playback_started"
        ));
    }

    #[test]
    fn turn_gate_silence_limit_preserves_vad_only_and_selects_active_timeout() {
        assert_eq!(
            App::recording_silence_limit(false, 0.5, 1000, 1600, true),
            Duration::from_millis(500)
        );
        assert_eq!(
            App::recording_silence_limit(true, 0.5, 1000, 1600, false),
            Duration::from_millis(1000)
        );
        assert_eq!(
            App::recording_silence_limit(true, 0.5, 1000, 1600, true),
            Duration::from_millis(1600)
        );
    }

    #[test]
    fn turn_commit_wait_only_adds_missing_stable_silence() {
        assert_eq!(App::turn_commit_wait_ms(467, 600), 133);
        assert_eq!(App::turn_commit_wait_ms(600, 600), 0);
        assert_eq!(App::turn_commit_wait_ms(720, 600), 0);
    }

    #[test]
    fn pending_turn_commit_rejects_resumed_or_stale_candidate_identity() {
        let pending = PendingTurnCommit {
            trace_id: Some("trace-1".to_string()),
            utterance_id: "utt-1".to_string(),
            candidate_seq: 2,
            speech_epoch: 1,
            audio_watermark: 16_000,
            asr_text: Some("测试".to_string()),
            asr_time_ms: Some(100.0),
            received_at: Instant::now(),
        };

        assert!(App::pending_turn_commit_matches(
            &pending, "utt-1", 16_000, 2, 1, true
        ));
        assert!(!App::pending_turn_commit_matches(
            &pending, "utt-1", 16_000, 2, 2, true
        ));
        assert!(!App::pending_turn_commit_matches(
            &pending, "utt-1", 16_000, 2, 1, false
        ));
        assert!(!App::pending_turn_commit_matches(
            &pending, "utt-2", 16_000, 2, 1, true
        ));
        assert!(!App::pending_turn_commit_matches(
            &pending, "utt-1", 15_999, 2, 1, true
        ));
    }
}
