use crate::{
    audio::{
        agc::apply_agc_and_gain,
        playback::{
            PlaybackCommand, PlaybackEvent, PlaybackGeneration, PlaybackHandle, PlaybackStats,
            PlaybackThread,
        },
    },
    audio_control::AudioDuckingState,
    audio_frontend::{
        protocol::{
            encode_s16le_payload, ensure_internal_audio_shape, repeat_upsample_16k_to_48k,
            upsample_16k_to_48k, upsample_16k_to_48k_with_next, AudioFrontendHeader, HEADER_BYTES,
            WIRE_PAYLOAD_BYTES,
        },
        tcp::{
            connect_with_timeout, wait_interruptibly, TCP_CONNECT_TIMEOUT, TCP_RECONNECT_INTERVAL,
        },
    },
    config::{AudioConfig, AudioFrontendUpsampler},
};
use anyhow::{Context, Result};
use crossbeam::channel::{bounded, Receiver, RecvTimeoutError, Sender, TryRecvError};
use rubato::{
    Resampler, SincFixedIn, SincInterpolationParameters, SincInterpolationType, WindowFunction,
};
use std::{
    collections::VecDeque,
    io::Write,
    net::TcpStream,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

const PLAYBACK_COMMAND_CAPACITY: usize = 500;
const PLAYBACK_EVENT_CAPACITY: usize = 32;
const IDLE_POLL_INTERVAL: Duration = Duration::from_millis(50);
const TCP_WRITE_TIMEOUT: Duration = Duration::from_millis(200);
const MAX_COMMANDS_PER_TICK: usize = 32;
const COMMAND_DRAIN_BUDGET: Duration = Duration::from_millis(2);
const SPEAKER_WRITE_AHEAD: Duration = Duration::from_millis(60);

fn write_ahead_deadline(now: Instant) -> Instant {
    now.checked_sub(SPEAKER_WRITE_AHEAD).unwrap_or(now)
}

fn schedule_missed(
    now: Instant,
    next_frame_at: Instant,
    schedule_epoch: Instant,
    frame_duration: Duration,
) -> bool {
    next_frame_at >= schedule_epoch && now > next_frame_at + frame_duration
}

pub fn start_playback_tcp(
    audio: &AudioConfig,
    fixed_gain: f32,
    agc_target_db: f32,
    ducking_state: AudioDuckingState,
) -> Result<PlaybackHandle> {
    ensure_internal_audio_shape(audio.sample_rate, audio.frame_duration_ms)?;

    let address = format!("{}:{}", audio.frontend_host, audio.frontend_speaker_port);
    let (command_tx, command_rx) = bounded::<PlaybackCommand>(PLAYBACK_COMMAND_CAPACITY);
    let (event_tx, event_rx) = bounded::<PlaybackEvent>(PLAYBACK_EVENT_CAPACITY);
    let stop = Arc::new(AtomicBool::new(false));
    let stop_for_thread = Arc::clone(&stop);
    let generation = PlaybackGeneration::default();
    let generation_for_thread = generation.clone();
    let frame_duration = Duration::from_millis(u64::from(audio.frame_duration_ms));
    let frontend_upsampler = audio.frontend_upsampler;

    let join = std::thread::spawn(move || {
        playback_supervisor(
            address,
            &command_rx,
            &event_tx,
            fixed_gain,
            agc_target_db,
            ducking_state,
            &stop_for_thread,
            frame_duration,
            frontend_upsampler,
            generation_for_thread,
        );
        tracing::info!("[AUDIO_FRONTEND] speaker TCP playback stopped");
    });

    Ok(PlaybackHandle::from_thread(
        command_tx,
        event_rx,
        generation,
        PlaybackThread::new(stop, join),
    ))
}

#[allow(clippy::too_many_arguments)]
fn playback_supervisor(
    address: String,
    command_rx: &Receiver<PlaybackCommand>,
    event_tx: &Sender<PlaybackEvent>,
    fixed_gain: f32,
    agc_target_db: f32,
    ducking_state: AudioDuckingState,
    stop: &AtomicBool,
    frame_duration: Duration,
    upsampler: AudioFrontendUpsampler,
    generation: PlaybackGeneration,
) {
    let mut queue = TcpPlayQueue::default();
    let mut speaker_upsampler = match SpeakerUpsampler::new(upsampler) {
        Ok(upsampler) => upsampler,
        Err(err) => {
            let _ = event_tx.try_send(PlaybackEvent::StreamError(format!(
                "audio_frontend speaker upsampler failed: {err}"
            )));
            return;
        }
    };
    if let Some(delay_samples) = speaker_upsampler.output_delay_samples() {
        tracing::info!(
            delay_samples,
            delay_ms = delay_samples as f64 / 48.0,
            "[AUDIO_FRONTEND] experimental sinc upsampler enabled"
        );
    }
    let started_at = Instant::now();
    let mut outage_notified = false;
    let mut failed_attempts = 0u64;

    while !stop.load(Ordering::Acquire) {
        match connect_playback_stream(&address) {
            Ok(mut stream) => {
                tracing::info!(address = %address, "[AUDIO_FRONTEND] speaker TCP playback connected");
                failed_attempts = 0;
                let mut seq = 0u64;
                if let Err(err) = playback_loop(
                    &mut stream,
                    command_rx,
                    event_tx,
                    fixed_gain,
                    agc_target_db,
                    ducking_state.clone(),
                    stop,
                    frame_duration,
                    upsampler,
                    generation.clone(),
                    &mut queue,
                    &mut speaker_upsampler,
                    &mut seq,
                    started_at,
                ) {
                    if stop.load(Ordering::Acquire) {
                        break;
                    }
                    let (_, new_generation) = generation.advance();
                    queue.sync_generation(new_generation, event_tx);
                    speaker_upsampler.reset();
                    tracing::warn!(address = %address, error = %err, "[AUDIO_FRONTEND] speaker TCP disconnected, retrying");
                    let _ = event_tx.try_send(PlaybackEvent::StreamError(format!(
                        "audio_frontend speaker TCP interrupted, retrying: {err}"
                    )));
                    outage_notified = true;
                } else {
                    break;
                }
            }
            Err(err) => {
                failed_attempts = failed_attempts.saturating_add(1);
                if !outage_notified {
                    let _ = event_tx.try_send(PlaybackEvent::StreamError(format!(
                        "audio_frontend speaker TCP unavailable, retrying: {err}"
                    )));
                    outage_notified = true;
                }
                if failed_attempts == 1 || failed_attempts % 20 == 0 {
                    tracing::warn!(address = %address, failed_attempts, error = %err, "[AUDIO_FRONTEND] speaker TCP connect failed, retrying");
                }
            }
        }
        wait_interruptibly(stop, TCP_RECONNECT_INTERVAL);
    }
}

fn connect_playback_stream(address: &str) -> Result<TcpStream> {
    let stream = connect_with_timeout(address, TCP_CONNECT_TIMEOUT)
        .with_context(|| format!("连接 audio_frontend speaker TCP 失败: {address}"))?;
    stream
        .set_nodelay(true)
        .context("设置 audio_frontend speaker TCP_NODELAY 失败")?;
    stream
        .set_write_timeout(Some(TCP_WRITE_TIMEOUT))
        .context("设置 audio_frontend speaker write timeout 失败")?;
    Ok(stream)
}

#[allow(clippy::too_many_arguments)]
fn playback_loop(
    stream: &mut TcpStream,
    command_rx: &Receiver<PlaybackCommand>,
    event_tx: &Sender<PlaybackEvent>,
    fixed_gain: f32,
    agc_target_db: f32,
    ducking_state: AudioDuckingState,
    stop: &AtomicBool,
    frame_duration: Duration,
    upsampler: AudioFrontendUpsampler,
    generation: PlaybackGeneration,
    queue: &mut TcpPlayQueue,
    speaker_upsampler: &mut SpeakerUpsampler,
    seq: &mut u64,
    started_at: Instant,
) -> Result<()> {
    let mut schedule_epoch = Instant::now();
    let mut next_frame_at = write_ahead_deadline(schedule_epoch);
    let mut last_send_at: Option<Instant> = None;
    let mut pending_write_duration_us: Option<u64> = None;
    while !stop.load(Ordering::Relaxed) {
        queue.sync_generation(generation.current(), event_tx);
        if !drain_commands(
            command_rx,
            event_tx,
            queue,
            fixed_gain,
            agc_target_db,
            MAX_COMMANDS_PER_TICK,
            COMMAND_DRAIN_BUDGET,
            &generation,
        ) {
            break;
        }
        if let Some(write_us) = pending_write_duration_us.take() {
            queue.record_tcp_write(write_us);
        }
        if queue.take_upsampler_reset_pending() {
            speaker_upsampler.reset();
        }
        emit_finished_if_ready(queue, event_tx);

        if !queue.should_render_frame() {
            match command_rx.recv_timeout(IDLE_POLL_INTERVAL) {
                Ok(command) => {
                    process_command(
                        command,
                        event_tx,
                        queue,
                        fixed_gain,
                        agc_target_db,
                        &generation,
                    );
                    schedule_epoch = Instant::now();
                    next_frame_at = write_ahead_deadline(schedule_epoch);
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => break,
            }
            if speaker_peer_closed(stream)? {
                anyhow::bail!("audio_frontend speaker TCP peer closed");
            }
            continue;
        }

        let now = Instant::now();
        if now < next_frame_at {
            match command_rx.recv_timeout(next_frame_at - now) {
                Ok(command) => {
                    process_command(
                        command,
                        event_tx,
                        queue,
                        fixed_gain,
                        agc_target_db,
                        &generation,
                    );
                    continue;
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => break,
            }
        }

        let send_started_at = Instant::now();
        // The initial write-ahead burst intentionally has deadlines before the
        // current playback round. Clamp telemetry to the round epoch so those
        // frames are not reported as scheduler lateness.
        let effective_deadline = next_frame_at.max(schedule_epoch);
        let late_ms = send_started_at
            .checked_duration_since(effective_deadline)
            .map(|duration| duration.as_millis().min(u128::from(u64::MAX)) as u64)
            .unwrap_or(0);
        let send_interval_us = last_send_at
            .and_then(|previous| send_started_at.checked_duration_since(previous))
            .map(|duration| duration.as_micros().min(u128::from(u64::MAX)) as u64)
            .unwrap_or(0);
        queue.record_tcp_frame(send_interval_us, late_ms);

        let duck_factor = ducking_state.current_factor();
        queue.sync_generation(generation.current(), event_tx);
        if queue.take_upsampler_reset_pending() {
            speaker_upsampler.reset();
        }
        let mut frame = queue.render_frame(duck_factor);
        if queue.generation != generation.current() {
            queue.sync_generation(generation.current(), event_tx);
            speaker_upsampler.reset();
            continue;
        }
        if frame.started {
            speaker_upsampler.reset();
        }
        let next_sample = if upsampler == AudioFrontendUpsampler::LinearLookahead {
            queue.peek_next_output_sample(duck_factor)
        } else {
            None
        };
        let write_started_at = Instant::now();
        write_speaker_frame(
            stream,
            *seq,
            elapsed_micros(started_at),
            &frame.samples,
            next_sample,
            speaker_upsampler,
        )?;
        let write_duration_us = elapsed_micros(write_started_at);
        if let Some(stats) = frame.finished.as_mut() {
            stats.tcp_max_write_us = stats.tcp_max_write_us.max(write_duration_us);
        } else {
            pending_write_duration_us = Some(write_duration_us);
        }
        last_send_at = Some(send_started_at);
        *seq = (*seq).wrapping_add(1);
        emit_render_events(frame, event_tx, queue.generation);

        next_frame_at += frame_duration;
        let now = Instant::now();
        if schedule_missed(now, next_frame_at, schedule_epoch, frame_duration) {
            // Re-establish bounded write-ahead after a scheduler stall instead
            // of permanently returning to just-in-time delivery.
            schedule_epoch = now;
            next_frame_at = write_ahead_deadline(now);
        }
    }

    Ok(())
}

fn speaker_peer_closed(stream: &TcpStream) -> Result<bool> {
    stream
        .set_nonblocking(true)
        .context("设置 audio_frontend speaker nonblocking probe 失败")?;
    let mut byte = [0u8; 1];
    let result = match stream.peek(&mut byte) {
        Ok(0) => Ok(true),
        Ok(_) => Ok(false),
        Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => Ok(false),
        Err(err) => Err(err).context("探测 audio_frontend speaker TCP 连接失败"),
    };
    stream
        .set_nonblocking(false)
        .context("恢复 audio_frontend speaker blocking 模式失败")?;
    result
}

#[allow(clippy::too_many_arguments)]
fn drain_commands(
    command_rx: &Receiver<PlaybackCommand>,
    event_tx: &Sender<PlaybackEvent>,
    queue: &mut TcpPlayQueue,
    fixed_gain: f32,
    agc_target_db: f32,
    max_commands: usize,
    time_budget: Duration,
    generation: &PlaybackGeneration,
) -> bool {
    let started_at = Instant::now();
    for processed in 0..max_commands {
        if processed > 0 && started_at.elapsed() >= time_budget {
            return true;
        }
        match command_rx.try_recv() {
            Ok(command) => process_command(
                command,
                event_tx,
                queue,
                fixed_gain,
                agc_target_db,
                generation,
            ),
            Err(TryRecvError::Empty) => return true,
            Err(TryRecvError::Disconnected) => return false,
        }
    }
    true
}

fn process_command(
    command: PlaybackCommand,
    event_tx: &Sender<PlaybackEvent>,
    queue: &mut TcpPlayQueue,
    fixed_gain: f32,
    agc_target_db: f32,
    generation: &PlaybackGeneration,
) {
    let current_generation = generation.current();
    queue.sync_generation(current_generation, event_tx);
    match command {
        PlaybackCommand::PlayPcm16k {
            generation: command_generation,
            samples,
        } if command_generation == current_generation => {
            let processed = apply_agc_and_gain(&samples, agc_target_db, 2.0, fixed_gain);
            if !generation.is_current(command_generation) {
                return;
            }
            if !queue.active_round && queue.samples.is_empty() {
                queue.stats = PlaybackStats::default();
                queue.active_round = true;
                queue.started_notified = false;
            }
            queue.stats.pushed_chunks += 1;
            queue.stats.pushed_samples += processed.len() as u64;
            queue.stats.max_buffered_samples = queue
                .stats
                .max_buffered_samples
                .max(queue.samples.len() + processed.len());
            queue.samples.extend(processed);
            queue.finished_notified = false;
        }
        PlaybackCommand::PlayFeedbackPcm16k {
            generation: command_generation,
            samples,
        } if command_generation == current_generation => {
            if !generation.is_current(command_generation) {
                return;
            }
            queue.feedback_samples.extend(samples);
        }
        PlaybackCommand::MarkDone {
            generation: command_generation,
        } if command_generation == current_generation => {
            if !generation.is_current(command_generation) {
                return;
            }
            queue.finish_requested = true;
        }
        PlaybackCommand::Interrupt { generation } if generation == current_generation => {
            let had_activity =
                queue.active_round || !queue.samples.is_empty() || stats_has_activity(&queue.stats);
            let stats = queue.stats.clone();
            queue.samples.clear();
            queue.feedback_samples.clear();
            queue.finish_requested = false;
            queue.finished_notified = true;
            queue.started_notified = false;
            queue.active_round = false;
            queue.stats = PlaybackStats::default();
            if had_activity {
                let _ = event_tx.try_send(PlaybackEvent::Interrupted { generation, stats });
            }
        }
        _ => {}
    }
}

fn emit_finished_if_ready(queue: &mut TcpPlayQueue, event_tx: &Sender<PlaybackEvent>) {
    if let Some(stats) = queue.take_finished_if_ready() {
        let _ = event_tx.try_send(PlaybackEvent::Finished {
            generation: queue.generation,
            stats,
        });
    }
}

fn emit_render_events(frame: RenderedFrame, event_tx: &Sender<PlaybackEvent>, generation: u64) {
    if frame.started {
        let _ = event_tx.try_send(PlaybackEvent::Started { generation });
    }
    if let Some(stats) = frame.finished {
        let _ = event_tx.try_send(PlaybackEvent::Finished { generation, stats });
    }
}

fn write_speaker_frame(
    stream: &mut TcpStream,
    seq: u64,
    timestamp_us: u64,
    samples_16k: &[i16],
    next_sample: Option<i16>,
    upsampler: &mut SpeakerUpsampler,
) -> Result<()> {
    let samples_48k = upsampler.process_frame(samples_16k, next_sample)?;
    let header = AudioFrontendHeader::speaker(seq, timestamp_us).encode();
    let payload = encode_s16le_payload(&samples_48k);
    let mut packet = [0u8; HEADER_BYTES + WIRE_PAYLOAD_BYTES];
    packet[..HEADER_BYTES].copy_from_slice(&header);
    packet[HEADER_BYTES..].copy_from_slice(&payload);
    stream
        .write_all(&packet)
        .context("写入 audio_frontend speaker frame 失败")?;
    Ok(())
}

struct SpeakerUpsampler {
    mode: AudioFrontendUpsampler,
    sinc: Option<SincFixedIn<f32>>,
    pending: VecDeque<i16>,
}

impl SpeakerUpsampler {
    fn new(mode: AudioFrontendUpsampler) -> Result<Self> {
        let sinc = if mode == AudioFrontendUpsampler::Sinc {
            Some(create_sinc_upsampler()?)
        } else {
            None
        };
        Ok(Self {
            mode,
            sinc,
            pending: VecDeque::new(),
        })
    }

    fn reset(&mut self) {
        self.pending.clear();
        if let Some(sinc) = self.sinc.as_mut() {
            sinc.reset();
        }
    }

    fn output_delay_samples(&self) -> Option<usize> {
        self.sinc.as_ref().map(Resampler::output_delay)
    }

    fn process_frame(
        &mut self,
        samples_16k: &[i16],
        next_sample: Option<i16>,
    ) -> Result<[i16; 480]> {
        match self.mode {
            AudioFrontendUpsampler::Repeat => repeat_upsample_16k_to_48k(samples_16k),
            AudioFrontendUpsampler::Linear => upsample_16k_to_48k(samples_16k),
            AudioFrontendUpsampler::LinearLookahead => {
                upsample_16k_to_48k_with_next(samples_16k, next_sample)
            }
            AudioFrontendUpsampler::Sinc => self.process_sinc_frame(samples_16k, next_sample),
        }
    }

    fn process_sinc_frame(
        &mut self,
        samples_16k: &[i16],
        next_sample: Option<i16>,
    ) -> Result<[i16; 480]> {
        if samples_16k.len() != 160 {
            anyhow::bail!(
                "audio_frontend sinc upsampler expects 160 samples at 16kHz, got {}",
                samples_16k.len()
            );
        }
        let input: Vec<f32> = samples_16k
            .iter()
            .map(|sample| *sample as f32 / 32768.0)
            .collect();
        let output = self
            .sinc
            .as_mut()
            .context("sinc upsampler is not initialized")?
            .process(&[input], None)
            .context("audio_frontend sinc upsample failed")?;
        if let Some(channel) = output.first() {
            self.pending
                .extend(channel.iter().map(|sample| f32_to_i16(*sample)));
        }
        if self.pending.len() < 480 {
            self.pending.clear();
            return upsample_16k_to_48k_with_next(samples_16k, next_sample);
        }

        let mut samples = [0i16; 480];
        for sample in samples.iter_mut() {
            *sample = self
                .pending
                .pop_front()
                .context("sinc upsampler pending buffer unexpectedly empty")?;
        }
        Ok(samples)
    }
}

fn create_sinc_upsampler() -> Result<SincFixedIn<f32>> {
    let params = SincInterpolationParameters {
        sinc_len: 128,
        f_cutoff: 0.95,
        interpolation: SincInterpolationType::Linear,
        oversampling_factor: 256,
        window: WindowFunction::BlackmanHarris2,
    };
    SincFixedIn::<f32>::new(3.0, 1.0, params, 160, 1)
        .context("创建 audio_frontend speaker sinc upsampler 失败")
}

fn elapsed_micros(started_at: Instant) -> u64 {
    started_at.elapsed().as_micros().min(u128::from(u64::MAX)) as u64
}

fn stats_has_activity(stats: &PlaybackStats) -> bool {
    stats.pushed_chunks > 0 || stats.zero_filled_samples > 0
}

#[derive(Default)]
struct TcpPlayQueue {
    samples: VecDeque<i16>,
    feedback_samples: VecDeque<i16>,
    finish_requested: bool,
    finished_notified: bool,
    started_notified: bool,
    active_round: bool,
    stats: PlaybackStats,
    generation: u64,
    upsampler_reset_pending: bool,
}

impl TcpPlayQueue {
    fn sync_generation(&mut self, generation: u64, event_tx: &Sender<PlaybackEvent>) {
        if self.generation == generation {
            return;
        }
        let interrupted_generation = self.generation;
        let stats = self.stats.clone();
        self.samples.clear();
        self.feedback_samples.clear();
        self.finish_requested = false;
        self.finished_notified = true;
        self.started_notified = false;
        self.active_round = false;
        self.stats = PlaybackStats::default();
        self.generation = generation;
        self.upsampler_reset_pending = true;
        let _ = event_tx.try_send(PlaybackEvent::Interrupted {
            generation: interrupted_generation,
            stats,
        });
    }

    fn take_upsampler_reset_pending(&mut self) -> bool {
        std::mem::take(&mut self.upsampler_reset_pending)
    }

    fn record_tcp_frame(&mut self, send_interval_us: u64, late_ms: u64) {
        if !self.active_round {
            return;
        }
        self.stats.tcp_frames_sent += 1;
        if late_ms > 0 {
            self.stats.tcp_late_frames += 1;
            self.stats.tcp_max_late_ms = self.stats.tcp_max_late_ms.max(late_ms);
        }
        self.stats.tcp_max_send_interval_us =
            self.stats.tcp_max_send_interval_us.max(send_interval_us);
    }

    fn record_tcp_write(&mut self, write_duration_us: u64) {
        if !self.active_round {
            return;
        }
        self.stats.tcp_max_write_us = self.stats.tcp_max_write_us.max(write_duration_us);
    }

    fn peek_next_output_sample(&self, duck_factor: f32) -> Option<i16> {
        let main_sample = self.samples.front().copied();
        let feedback_sample = self.feedback_samples.front().copied();
        if main_sample.is_none() && feedback_sample.is_none() {
            return None;
        }

        let mut mixed_value = 0.0f32;
        if let Some(value) = main_sample {
            mixed_value += value as f32 / 32768.0;
        }
        if let Some(value) = feedback_sample {
            mixed_value += value as f32 / 32768.0;
        }
        Some(f32_to_i16(mixed_value * duck_factor))
    }

    fn should_render_frame(&self) -> bool {
        !self.samples.is_empty()
            || !self.feedback_samples.is_empty()
            || (self.active_round && !self.finish_requested)
    }

    fn render_frame(&mut self, duck_factor: f32) -> RenderedFrame {
        let mut rendered = RenderedFrame {
            samples: [0i16; 160],
            ..RenderedFrame::default()
        };
        let mut zero_filled = 0usize;

        for sample in rendered.samples.iter_mut() {
            let main_sample = self.samples.pop_front();
            let feedback_sample = self.feedback_samples.pop_front();
            let mut mixed_value = 0.0f32;

            if let Some(value) = main_sample {
                if !self.started_notified {
                    self.started_notified = true;
                    rendered.started = true;
                }
                mixed_value += value as f32 / 32768.0;
            } else if feedback_sample.is_none() && !self.finish_requested && self.active_round {
                zero_filled += 1;
            }

            if let Some(value) = feedback_sample {
                mixed_value += value as f32 / 32768.0;
            }

            *sample = f32_to_i16(mixed_value * duck_factor);
        }

        if zero_filled > 0 {
            self.stats.underrun_callbacks += 1;
            self.stats.zero_filled_samples += zero_filled as u64;
        }
        rendered.finished = self.take_finished_if_ready();
        rendered
    }

    fn take_finished_if_ready(&mut self) -> Option<PlaybackStats> {
        if self.samples.is_empty() && self.finish_requested && !self.finished_notified {
            let stats = self.stats.clone();
            self.finish_requested = false;
            self.finished_notified = true;
            self.started_notified = false;
            self.active_round = false;
            self.stats = PlaybackStats::default();
            Some(stats)
        } else {
            None
        }
    }
}

struct RenderedFrame {
    samples: [i16; 160],
    started: bool,
    finished: Option<PlaybackStats>,
}

impl Default for RenderedFrame {
    fn default() -> Self {
        Self {
            samples: [0i16; 160],
            started: false,
            finished: None,
        }
    }
}

fn f32_to_i16(value: f32) -> i16 {
    let scaled = (value.clamp(-1.0, 1.0) * 32767.0).round();
    scaled.clamp(i16::MIN as f32, i16::MAX as f32) as i16
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        audio_frontend::protocol::{
            decode_s16le_payload, AudioFrontendHeader, FRAME_TYPE_SPEAKER, HEADER_BYTES,
            WIRE_PAYLOAD_BYTES,
        },
        config::{AudioConfig, AudioFrontendMode, AudioFrontendUpsampler},
    };
    use std::{
        io::Read,
        net::{TcpListener, TcpStream},
        sync::mpsc,
        thread,
        time::{Duration, SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn drain_commands_respects_per_tick_limit() {
        let (command_tx, command_rx) = bounded(8);
        let (event_tx, _event_rx) = bounded(8);
        let generation = PlaybackGeneration::default();
        for _ in 0..3 {
            command_tx
                .send(PlaybackCommand::PlayPcm16k {
                    generation: generation.current(),
                    samples: vec![0; 160],
                })
                .expect("queue playback command");
        }
        let mut queue = TcpPlayQueue::default();

        assert!(drain_commands(
            &command_rx,
            &event_tx,
            &mut queue,
            1.0,
            -25.0,
            2,
            Duration::from_secs(1),
            &generation,
        ));

        assert_eq!(queue.stats.pushed_chunks, 2);
        assert_eq!(command_rx.len(), 1);
    }

    #[test]
    fn drain_commands_makes_progress_with_zero_time_budget() {
        let (command_tx, command_rx) = bounded(8);
        let (event_tx, _event_rx) = bounded(8);
        let generation = PlaybackGeneration::default();
        for _ in 0..3 {
            command_tx
                .send(PlaybackCommand::PlayPcm16k {
                    generation: generation.current(),
                    samples: vec![0; 160],
                })
                .expect("queue playback command");
        }
        let mut queue = TcpPlayQueue::default();

        assert!(drain_commands(
            &command_rx,
            &event_tx,
            &mut queue,
            1.0,
            -25.0,
            8,
            Duration::ZERO,
            &generation,
        ));

        assert_eq!(queue.stats.pushed_chunks, 1);
        assert_eq!(command_rx.len(), 2);
    }

    #[test]
    fn speaker_schedule_establishes_sixty_ms_write_ahead() {
        let epoch = Instant::now();
        let mut deadline = write_ahead_deadline(epoch);
        let frame_duration = Duration::from_millis(10);

        for _ in 0..7 {
            assert!(deadline <= epoch);
            deadline += frame_duration;
        }
        assert!(deadline > epoch);
    }

    #[test]
    fn initial_write_ahead_is_not_mistaken_for_scheduler_lateness() {
        let epoch = Instant::now();
        let frame_duration = Duration::from_millis(10);
        let initial_deadline = write_ahead_deadline(epoch);
        assert!(!schedule_missed(
            epoch,
            initial_deadline,
            epoch,
            frame_duration
        ));

        let caught_up_deadline = epoch + frame_duration;
        let stalled_now = caught_up_deadline + frame_duration + frame_duration;
        assert!(schedule_missed(
            stalled_now,
            caught_up_deadline,
            epoch,
            frame_duration
        ));
    }

    #[test]
    fn tcp_queue_rejects_old_audio_and_finish_after_generation_change() {
        let generation = PlaybackGeneration::default();
        let old_generation = generation.current();
        let (event_tx, _event_rx) = bounded(8);
        let mut queue = TcpPlayQueue {
            samples: VecDeque::from(vec![10_000; 160]),
            feedback_samples: VecDeque::from(vec![5_000; 160]),
            active_round: true,
            stats: PlaybackStats {
                pushed_chunks: 1,
                pushed_samples: 160,
                ..PlaybackStats::default()
            },
            ..TcpPlayQueue::default()
        };
        generation.advance();
        let new_generation = generation.current();

        process_command(
            PlaybackCommand::PlayFeedbackPcm16k {
                generation: old_generation,
                samples: vec![5; 160],
            },
            &event_tx,
            &mut queue,
            1.0,
            -25.0,
            &generation,
        );
        process_command(
            PlaybackCommand::PlayPcm16k {
                generation: old_generation,
                samples: vec![1; 160],
            },
            &event_tx,
            &mut queue,
            1.0,
            -25.0,
            &generation,
        );
        process_command(
            PlaybackCommand::PlayPcm16k {
                generation: new_generation,
                samples: vec![2; 160],
            },
            &event_tx,
            &mut queue,
            1.0,
            -25.0,
            &generation,
        );
        process_command(
            PlaybackCommand::MarkDone {
                generation: old_generation,
            },
            &event_tx,
            &mut queue,
            1.0,
            -25.0,
            &generation,
        );

        assert_eq!(queue.generation, new_generation);
        assert_eq!(queue.stats.pushed_chunks, 1);
        assert_eq!(queue.stats.pushed_samples, 160);
        assert_eq!(queue.samples.len(), 160);
        assert!(queue.feedback_samples.is_empty());
        assert!(!queue.finish_requested);

        process_command(
            PlaybackCommand::MarkDone {
                generation: new_generation,
            },
            &event_tx,
            &mut queue,
            1.0,
            -25.0,
            &generation,
        );
        assert!(queue.finish_requested);
    }

    #[test]
    fn empty_tcp_queue_generation_change_emits_old_generation_interrupt() {
        let generation = PlaybackGeneration::default();
        let mut queue = TcpPlayQueue::default();
        let (event_tx, event_rx) = bounded(1);
        generation.advance();

        queue.sync_generation(generation.current(), &event_tx);

        assert!(matches!(
            event_rx.try_recv(),
            Ok(PlaybackEvent::Interrupted {
                generation: 0,
                stats: PlaybackStats {
                    pushed_chunks: 0,
                    pushed_samples: 0,
                    ..
                },
            })
        ));
    }

    #[test]
    fn tcp_playback_sends_speaker_frame_and_finishes() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("local addr").port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            let mut header_bytes = [0u8; HEADER_BYTES];
            let mut payload = [0u8; WIRE_PAYLOAD_BYTES];
            stream.read_exact(&mut header_bytes).expect("read header");
            stream.read_exact(&mut payload).expect("read payload");

            let header = AudioFrontendHeader::decode(&header_bytes);
            header.validate(FRAME_TYPE_SPEAKER).expect("speaker header");
            assert_eq!(header.seq, 0);
            let samples = decode_s16le_payload(&payload);
            assert!(samples.iter().all(|sample| *sample == 0));
        });

        let audio = test_audio_config(port);
        let playback =
            start_playback_tcp(&audio, 1.0, -25.0, AudioDuckingState::new(15_000, 60_000))
                .expect("start tcp playback");
        let generation = playback.generation.current();
        playback
            .command_tx
            .send(PlaybackCommand::PlayPcm16k {
                generation,
                samples: vec![0; 160],
            })
            .expect("send playback command");
        playback
            .command_tx
            .send(PlaybackCommand::MarkDone { generation })
            .expect("send mark done");

        assert!(matches!(
            playback.event_rx.recv_timeout(Duration::from_secs(2)),
            Ok(PlaybackEvent::Started { generation: 0 })
        ));
        assert!(matches!(
            playback.event_rx.recv_timeout(Duration::from_secs(2)),
            Ok(PlaybackEvent::Finished { generation: 0, .. })
        ));
        drop(playback);
        server.join().expect("server thread");
    }

    #[test]
    fn tcp_playback_reconnects_after_peer_restart() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        listener
            .set_nonblocking(true)
            .expect("set listener nonblocking");
        let port = listener.local_addr().expect("local addr").port();
        let (packet_tx, packet_rx) = mpsc::channel();
        let server = thread::spawn(move || {
            for connection_index in 0..2 {
                let mut stream = accept_with_timeout(&listener, Duration::from_secs(4));
                let mut packet = [0u8; HEADER_BYTES + WIRE_PAYLOAD_BYTES];
                stream.read_exact(&mut packet).expect("read speaker packet");
                packet_tx
                    .send(connection_index)
                    .expect("report received packet");
            }
        });

        let audio = test_audio_config(port);
        let playback =
            start_playback_tcp(&audio, 1.0, -25.0, AudioDuckingState::new(15_000, 60_000))
                .expect("start tcp playback");
        let old_generation = playback.generation.current();
        playback
            .command_tx
            .send(PlaybackCommand::PlayFeedbackPcm16k {
                generation: old_generation,
                samples: vec![100; 160],
            })
            .expect("send first feedback");
        assert_eq!(
            packet_rx
                .recv_timeout(Duration::from_secs(2))
                .expect("first packet"),
            0
        );

        let generation_deadline = Instant::now() + Duration::from_secs(3);
        while playback.generation.current() == old_generation
            && Instant::now() < generation_deadline
        {
            thread::sleep(Duration::from_millis(10));
        }
        let new_generation = playback.generation.current();
        assert_ne!(new_generation, old_generation);
        playback
            .command_tx
            .send(PlaybackCommand::PlayFeedbackPcm16k {
                generation: new_generation,
                samples: vec![200; 160],
            })
            .expect("send feedback after reconnect");
        assert_eq!(
            packet_rx
                .recv_timeout(Duration::from_secs(3))
                .expect("packet after reconnect"),
            1
        );

        drop(playback);
        server.join().expect("server thread");
    }

    fn accept_with_timeout(listener: &TcpListener, timeout: Duration) -> TcpStream {
        let deadline = Instant::now() + timeout;
        loop {
            match listener.accept() {
                Ok((stream, _)) => {
                    stream
                        .set_nonblocking(false)
                        .expect("set accepted stream blocking");
                    stream
                        .set_read_timeout(Some(timeout))
                        .expect("set accepted stream read timeout");
                    return stream;
                }
                Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(Instant::now() < deadline, "timed out accepting client");
                    thread::sleep(Duration::from_millis(10));
                }
                Err(err) => panic!("accept client failed: {err}"),
            }
        }
    }

    fn test_audio_config(speaker_port: u16) -> AudioConfig {
        AudioConfig {
            sample_rate: 16_000,
            channels: 1,
            frame_duration_ms: 10,
            tts_source_rate: 16_000,
            playback_buffer_frames: 2048,
            frontend_mode: AudioFrontendMode::Tcp,
            frontend_host: "127.0.0.1".to_string(),
            frontend_mic_port: 39_001,
            frontend_speaker_port: speaker_port,
            frontend_control_port: 39_003,
            frontend_mute_mic_during_playback: false,
            tts_barge_in_with_aec: true,
            natural_barge_in_enabled: false,
            frontend_upsampler: AudioFrontendUpsampler::Sinc,
            mic_device_name: None,
            speaker_device_name: None,
            pause_capture_while_waiting: true,
            wake_capture_prebuffer_ms: 800,
        }
    }

    #[test]
    fn render_frame_repeats_feedback_without_round_events() {
        let mut queue = TcpPlayQueue {
            feedback_samples: VecDeque::from(vec![100, -100]),
            ..TcpPlayQueue::default()
        };

        let frame = queue.render_frame(1.0);

        assert!(!frame.started);
        assert!(frame.finished.is_none());
        assert_eq!(frame.samples[0], 100);
        assert_eq!(frame.samples[1], -100);
    }

    #[test]
    fn elapsed_micros_uses_monotonic_time() {
        let now = Instant::now();
        let value = elapsed_micros(now);
        let wall = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();

        assert!(value < wall.as_micros() as u64);
    }

    #[test]
    fn sinc_upsampler_outputs_fixed_wire_frame() {
        let mut upsampler =
            SpeakerUpsampler::new(AudioFrontendUpsampler::Sinc).expect("create upsampler");
        let mut samples = vec![0i16; 160];
        samples[0] = 1000;
        samples[80] = -1000;

        let frame = upsampler
            .process_frame(&samples, None)
            .expect("process sinc frame");
        let second_frame = upsampler
            .process_frame(&samples, None)
            .expect("process second sinc frame");

        assert_eq!(frame.len(), 480);
        assert_eq!(second_frame.len(), 480);
    }

    #[test]
    fn sinc_reset_discards_cancelled_round_tail() {
        let mut upsampler =
            SpeakerUpsampler::new(AudioFrontendUpsampler::Sinc).expect("create upsampler");
        let mut impulse = vec![0i16; 160];
        impulse[159] = i16::MAX;

        for _ in 0..3 {
            upsampler
                .process_frame(&impulse, None)
                .expect("prime sinc filter");
        }
        upsampler.reset();

        for _ in 0..3 {
            let frame = upsampler
                .process_frame(&[0i16; 160], None)
                .expect("process silence after reset");
            assert!(frame.iter().all(|sample| *sample == 0));
        }
    }
}
