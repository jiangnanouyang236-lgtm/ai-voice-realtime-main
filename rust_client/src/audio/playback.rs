use crate::{
    audio::agc::apply_agc_and_gain, audio_control::AudioDuckingState, config::AudioConfig,
};
use anyhow::{Context, Result};
use cpal::{
    traits::{DeviceTrait, HostTrait, StreamTrait},
    SampleFormat, Stream, StreamConfig,
};
use crossbeam::channel::{bounded, Receiver, Sender};
use std::collections::VecDeque;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, Mutex,
};
use std::thread::JoinHandle;

#[derive(Debug, Clone)]
pub enum PlaybackCommand {
    PlayPcm16k { generation: u64, samples: Vec<i16> },
    PlayFeedbackPcm16k { generation: u64, samples: Vec<i16> },
    MarkDone { generation: u64 },
    Interrupt { generation: u64 },
}

#[derive(Clone, Default)]
pub struct PlaybackGeneration(Arc<AtomicU64>);

impl PlaybackGeneration {
    pub fn current(&self) -> u64 {
        self.0.load(Ordering::Acquire)
    }

    pub fn advance(&self) -> (u64, u64) {
        let previous = self.0.fetch_add(1, Ordering::AcqRel);
        (previous, previous.wrapping_add(1))
    }

    pub fn is_current(&self, generation: u64) -> bool {
        self.current() == generation
    }
}

#[derive(Debug, Clone)]
pub enum PlaybackEvent {
    Started {
        generation: u64,
    },
    Finished {
        generation: u64,
        stats: PlaybackStats,
    },
    Interrupted {
        generation: u64,
        stats: PlaybackStats,
    },
    StreamError(String),
}

#[derive(Debug, Clone, Default)]
pub struct PlaybackStats {
    pub pushed_chunks: u64,
    pub pushed_samples: u64,
    pub underrun_callbacks: u64,
    pub zero_filled_samples: u64,
    pub max_buffered_samples: usize,
    pub tcp_frames_sent: u64,
    pub tcp_late_frames: u64,
    pub tcp_max_late_ms: u64,
    pub tcp_max_send_interval_us: u64,
    pub tcp_max_write_us: u64,
}

impl PlaybackStats {
    fn has_activity(&self) -> bool {
        self.pushed_chunks > 0 || self.zero_filled_samples > 0
    }
}

pub struct PlaybackHandle {
    pub command_tx: Sender<PlaybackCommand>,
    pub event_rx: Receiver<PlaybackEvent>,
    pub generation: PlaybackGeneration,
    _source: PlaybackSource,
}

pub enum PlaybackSource {
    Cpal { _stream: Stream },
    Thread { _thread: PlaybackThread },
}

pub struct PlaybackThread {
    stop: Arc<AtomicBool>,
    join: Option<JoinHandle<()>>,
}

impl PlaybackThread {
    pub fn new(stop: Arc<AtomicBool>, join: JoinHandle<()>) -> Self {
        Self {
            stop,
            join: Some(join),
        }
    }
}

impl Drop for PlaybackThread {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(join) = self.join.take() {
            let _ = join.join();
        }
    }
}

impl PlaybackHandle {
    pub fn from_thread(
        command_tx: Sender<PlaybackCommand>,
        event_rx: Receiver<PlaybackEvent>,
        generation: PlaybackGeneration,
        thread: PlaybackThread,
    ) -> Self {
        Self {
            command_tx,
            event_rx,
            generation,
            _source: PlaybackSource::Thread { _thread: thread },
        }
    }
}

pub fn start_playback(
    audio: &AudioConfig,
    fixed_gain: f32,
    agc_target_db: f32,
    ducking_state: AudioDuckingState,
) -> Result<PlaybackHandle> {
    let host = cpal::default_host();
    let device = if let Some(prefix) = audio.speaker_device_name.as_deref() {
        find_output_device(&host, prefix).or_else(|_| {
            tracing::warn!("[AUDIO][WARN] 未找到喇叭 {}，回退默认输出设备", prefix);
            host.default_output_device().context("没有找到默认输出设备")
        })?
    } else {
        host.default_output_device()
            .context("没有找到默认输出设备")?
    };

    let supported = device
        .default_output_config()
        .context("获取默认输出配置失败")?;
    let device_name = device.name().unwrap_or_else(|_| "<unknown>".to_string());
    tracing::info!(
        "[AUDIO] 输出设备: {} / sample_format={:?} / rate={} / channels={} / buffer_frames={}",
        device_name,
        supported.sample_format(),
        supported.sample_rate().0,
        supported.channels(),
        audio.playback_buffer_frames,
    );

    let config = StreamConfig {
        channels: 1,
        sample_rate: cpal::SampleRate(audio.sample_rate),
        buffer_size: cpal::BufferSize::Fixed(audio.playback_buffer_frames),
    };

    let (command_tx, command_rx) = bounded::<PlaybackCommand>(500);
    let (event_tx, event_rx) = bounded::<PlaybackEvent>(32);
    let queue = Arc::new(Mutex::new(PlayQueue::default()));
    let generation = PlaybackGeneration::default();

    let queue_for_thread = Arc::clone(&queue);
    let event_tx_for_thread = event_tx.clone();
    let generation_for_thread = generation.clone();
    std::thread::spawn(move || {
        playback_command_loop(
            command_rx,
            event_tx_for_thread,
            queue_for_thread,
            fixed_gain,
            agc_target_db,
            generation_for_thread,
        )
    });

    let queue_for_cb = Arc::clone(&queue);
    let event_tx_err = event_tx.clone();
    let generation_for_cb = generation.clone();
    let stream = match supported.sample_format() {
        SampleFormat::F32 => build_output_stream::<f32>(
            &device,
            &config,
            queue_for_cb,
            event_tx_err,
            ducking_state.clone(),
            generation_for_cb,
        )?,
        SampleFormat::I16 => build_output_stream::<i16>(
            &device,
            &config,
            queue_for_cb,
            event_tx_err,
            ducking_state.clone(),
            generation_for_cb,
        )?,
        SampleFormat::U16 => build_output_stream::<u16>(
            &device,
            &config,
            queue_for_cb,
            event_tx_err,
            ducking_state.clone(),
            generation_for_cb,
        )?,
        other => anyhow::bail!("不支持的输出采样格式: {other:?}"),
    };

    stream.play().context("启动输出流失败")?;
    Ok(PlaybackHandle {
        command_tx,
        event_rx,
        generation,
        _source: PlaybackSource::Cpal { _stream: stream },
    })
}

#[derive(Default)]
struct PlayQueue {
    samples: VecDeque<i16>,
    feedback_samples: VecDeque<i16>,
    finish_requested: bool,
    finished_notified: bool,
    started_notified: bool,
    active_round: bool,
    stats: PlaybackStats,
    generation: u64,
}

fn playback_command_loop(
    command_rx: Receiver<PlaybackCommand>,
    event_tx: Sender<PlaybackEvent>,
    queue: Arc<Mutex<PlayQueue>>,
    fixed_gain: f32,
    agc_target_db: f32,
    generation: PlaybackGeneration,
) {
    while let Ok(command) = command_rx.recv() {
        let current_generation = generation.current();
        match command {
            PlaybackCommand::PlayPcm16k {
                generation: command_generation,
                samples,
            } if command_generation == current_generation => {
                let processed = apply_agc_and_gain(&samples, agc_target_db, 2.0, fixed_gain);
                if !generation.is_current(command_generation) {
                    continue;
                }
                let mut q = queue.lock().unwrap();
                emit_generation_interrupt(&mut q, generation.current(), &event_tx);
                if q.generation != command_generation {
                    continue;
                }
                if !q.active_round && q.samples.is_empty() {
                    q.stats = PlaybackStats::default();
                    q.active_round = true;
                    q.started_notified = false;
                }
                q.stats.pushed_chunks += 1;
                q.stats.pushed_samples += processed.len() as u64;
                q.stats.max_buffered_samples = q
                    .stats
                    .max_buffered_samples
                    .max(q.samples.len() + processed.len());
                q.samples.extend(processed);
                q.finished_notified = false;
            }
            PlaybackCommand::PlayFeedbackPcm16k {
                generation: command_generation,
                samples,
            } if command_generation == current_generation => {
                let mut q = queue.lock().unwrap();
                emit_generation_interrupt(&mut q, generation.current(), &event_tx);
                if q.generation != command_generation {
                    continue;
                }
                q.feedback_samples.extend(samples);
            }
            PlaybackCommand::MarkDone {
                generation: command_generation,
            } if command_generation == current_generation => {
                let mut q = queue.lock().unwrap();
                emit_generation_interrupt(&mut q, generation.current(), &event_tx);
                if q.generation != command_generation {
                    continue;
                }
                q.finish_requested = true;
            }
            PlaybackCommand::Interrupt {
                generation: command_generation,
            } if command_generation == current_generation => {
                let mut q = queue.lock().unwrap();
                emit_generation_interrupt(&mut q, generation.current(), &event_tx);
                if q.generation != command_generation {
                    continue;
                }
                let had_activity =
                    q.active_round || !q.samples.is_empty() || q.stats.has_activity();
                let stats = q.stats.clone();
                q.samples.clear();
                q.feedback_samples.clear();
                q.finish_requested = false;
                q.finished_notified = true;
                q.started_notified = false;
                q.active_round = false;
                q.stats = PlaybackStats::default();
                if had_activity {
                    let _ = event_tx.try_send(PlaybackEvent::Interrupted {
                        generation: command_generation,
                        stats,
                    });
                }
            }
            _ => {}
        }
    }
}

fn emit_generation_interrupt(
    queue: &mut PlayQueue,
    generation: u64,
    event_tx: &Sender<PlaybackEvent>,
) {
    if queue.generation == generation {
        return;
    }
    let interrupted_generation = queue.generation;
    let stats = queue.stats.clone();
    queue.samples.clear();
    queue.feedback_samples.clear();
    queue.finish_requested = false;
    queue.finished_notified = true;
    queue.started_notified = false;
    queue.active_round = false;
    queue.stats = PlaybackStats::default();
    queue.generation = generation;
    let _ = event_tx.try_send(PlaybackEvent::Interrupted {
        generation: interrupted_generation,
        stats,
    });
}

fn build_output_stream<T>(
    device: &cpal::Device,
    config: &StreamConfig,
    queue: Arc<Mutex<PlayQueue>>,
    event_tx: Sender<PlaybackEvent>,
    ducking_state: AudioDuckingState,
    generation: PlaybackGeneration,
) -> Result<Stream>
where
    T: cpal::SizedSample + cpal::FromSample<f32>,
{
    let event_tx_err = event_tx.clone();
    let stream = device.build_output_stream(
        config,
        move |data: &mut [T], _: &cpal::OutputCallbackInfo| {
            let mut q = queue.lock().unwrap();
            emit_generation_interrupt(&mut q, generation.current(), &event_tx);
            let duck_factor = ducking_state.current_factor();
            let outcome = render_play_queue(&mut q, data, duck_factor);
            if outcome.started {
                let _ = event_tx.try_send(PlaybackEvent::Started {
                    generation: q.generation,
                });
            }
            if let Some(stats) = outcome.finished {
                let _ = event_tx.try_send(PlaybackEvent::Finished {
                    generation: q.generation,
                    stats,
                });
            }
        },
        move |err| {
            let _ = event_tx_err.try_send(PlaybackEvent::StreamError(err.to_string()));
        },
        None,
    )?;
    Ok(stream)
}

#[derive(Default)]
struct RenderOutcome {
    started: bool,
    finished: Option<PlaybackStats>,
}

fn render_play_queue<T>(queue: &mut PlayQueue, data: &mut [T], duck_factor: f32) -> RenderOutcome
where
    T: cpal::SizedSample + cpal::FromSample<f32>,
{
    let mut outcome = RenderOutcome::default();
    let mut zero_filled = 0usize;
    for sample in data.iter_mut() {
        let main_sample = queue.samples.pop_front();
        let feedback_sample = queue.feedback_samples.pop_front();
        let mut mixed_value = 0.0f32;

        if let Some(v) = main_sample {
            if !queue.started_notified {
                queue.started_notified = true;
                outcome.started = true;
            }
            mixed_value += v as f32 / 32768.0;
        } else if feedback_sample.is_none() && !queue.finish_requested && queue.active_round {
            zero_filled += 1;
        }

        if let Some(v) = feedback_sample {
            mixed_value += v as f32 / 32768.0;
        }

        *sample = T::from_sample((mixed_value * duck_factor).clamp(-1.0, 1.0));
    }
    if zero_filled > 0 {
        queue.stats.underrun_callbacks += 1;
        queue.stats.zero_filled_samples += zero_filled as u64;
    }
    if queue.samples.is_empty() && queue.finish_requested && !queue.finished_notified {
        let stats = queue.stats.clone();
        queue.finish_requested = false;
        queue.finished_notified = true;
        queue.started_notified = false;
        queue.active_round = false;
        queue.stats = PlaybackStats::default();
        outcome.finished = Some(stats);
    }
    outcome
}

fn find_output_device(host: &cpal::Host, name_prefix: &str) -> Result<cpal::Device> {
    let needle = name_prefix.to_lowercase();
    for device in host.output_devices().context("枚举输出设备失败")? {
        let Ok(name) = device.name() else {
            continue;
        };
        if name.to_lowercase().contains(&needle) {
            tracing::info!("[AUDIO] 使用输出设备: {}", name);
            return Ok(device);
        }
    }
    anyhow::bail!("未找到输出设备: {name_prefix}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mark_done_waits_until_local_buffer_is_drained() {
        let mut queue = PlayQueue {
            samples: VecDeque::from(vec![100, 200, 300, 400]),
            feedback_samples: VecDeque::new(),
            finish_requested: true,
            finished_notified: false,
            started_notified: false,
            active_round: true,
            stats: PlaybackStats {
                pushed_chunks: 1,
                pushed_samples: 4,
                max_buffered_samples: 4,
                ..PlaybackStats::default()
            },
            generation: 0,
        };

        let mut first_callback = [0.0f32; 2];
        let first = render_play_queue(&mut queue, &mut first_callback, 1.0);
        assert!(first.started);
        assert!(first.finished.is_none());
        assert_eq!(2, queue.samples.len());
        assert!(queue.finish_requested);
        assert!(!queue.finished_notified);

        let mut second_callback = [0.0f32; 1];
        let second = render_play_queue(&mut queue, &mut second_callback, 1.0);
        assert!(!second.started);
        assert!(second.finished.is_none());
        assert_eq!(1, queue.samples.len());
        assert!(queue.finish_requested);
        assert!(!queue.finished_notified);

        let mut final_callback = [0.0f32; 1];
        let final_outcome = render_play_queue(&mut queue, &mut final_callback, 1.0);
        assert!(!final_outcome.started);
        let stats = final_outcome
            .finished
            .expect("playback should finish only after the final local sample is rendered");

        assert_eq!(1, stats.pushed_chunks);
        assert_eq!(4, stats.pushed_samples);
        assert_eq!(4, stats.max_buffered_samples);
        assert!(queue.samples.is_empty());
        assert!(!queue.finish_requested);
        assert!(queue.finished_notified);
        assert!(!queue.active_round);
    }

    #[test]
    fn feedback_samples_do_not_start_or_finish_a_playback_round() {
        let mut queue = PlayQueue {
            feedback_samples: VecDeque::from(vec![100, 200]),
            ..PlayQueue::default()
        };

        let mut output = [0.0f32; 2];
        let outcome = render_play_queue(&mut queue, &mut output, 1.0);

        assert!(!outcome.started);
        assert!(outcome.finished.is_none());
        assert_eq!(0, queue.stats.pushed_chunks);
        assert_eq!(0, queue.stats.pushed_samples);
        assert!(queue.feedback_samples.is_empty());
    }

    #[test]
    fn generation_change_cancels_within_callback_when_command_queue_is_full() {
        let generation = PlaybackGeneration::default();
        let old_generation = generation.current();
        let (command_tx, _command_rx) = bounded(1);
        command_tx
            .send(PlaybackCommand::PlayPcm16k {
                generation: old_generation,
                samples: vec![1; 160],
            })
            .expect("fill command queue");
        let (_, new_generation) = generation.advance();
        assert!(command_tx
            .try_send(PlaybackCommand::Interrupt {
                generation: new_generation,
            })
            .is_err());

        let mut queue = PlayQueue {
            samples: VecDeque::from(vec![10_000; 160]),
            feedback_samples: VecDeque::from(vec![5_000; 160]),
            active_round: true,
            stats: PlaybackStats {
                pushed_chunks: 1,
                pushed_samples: 160,
                ..PlaybackStats::default()
            },
            ..PlayQueue::default()
        };
        let (event_tx, event_rx) = bounded(1);
        emit_generation_interrupt(&mut queue, generation.current(), &event_tx);
        let mut output = [1.0f32; 160];
        let outcome = render_play_queue(&mut queue, &mut output, 1.0);

        assert!(output.iter().all(|sample| *sample == 0.0));
        assert!(!outcome.started);
        assert!(outcome.finished.is_none());
        assert!(queue.samples.is_empty());
        assert!(queue.feedback_samples.is_empty());
        assert!(matches!(
            event_rx.try_recv(),
            Ok(PlaybackEvent::Interrupted { generation: 0, .. })
        ));
    }

    #[test]
    fn queued_old_feedback_is_rejected_after_generation_change() {
        let generation = PlaybackGeneration::default();
        let old_generation = generation.current();
        generation.advance();
        let (command_tx, command_rx) = bounded(1);
        let (event_tx, _event_rx) = bounded(1);
        let queue = Arc::new(Mutex::new(PlayQueue::default()));
        command_tx
            .send(PlaybackCommand::PlayFeedbackPcm16k {
                generation: old_generation,
                samples: vec![1; 160],
            })
            .expect("queue old feedback");
        drop(command_tx);

        playback_command_loop(
            command_rx,
            event_tx,
            Arc::clone(&queue),
            1.0,
            -25.0,
            generation,
        );

        assert!(queue.lock().unwrap().feedback_samples.is_empty());
    }

    #[test]
    fn empty_queue_generation_change_emits_old_generation_interrupt() {
        let generation = PlaybackGeneration::default();
        let mut queue = PlayQueue::default();
        let (event_tx, event_rx) = bounded(1);
        generation.advance();

        emit_generation_interrupt(&mut queue, generation.current(), &event_tx);

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
}
