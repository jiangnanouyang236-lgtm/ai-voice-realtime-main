use crate::{
    audio::{devices::find_input_device, resample::InputResampler},
    config::AudioConfig,
};
use anyhow::{Context, Result};
use cpal::{
    traits::{DeviceTrait, StreamTrait},
    Sample, SampleFormat, Stream, StreamConfig,
};
use crossbeam::{
    channel::{bounded, Receiver, Sender, TryRecvError},
    queue::ArrayQueue,
};
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc,
};
use std::thread::JoinHandle;

#[derive(Debug, Clone)]
pub enum CaptureEvent {
    StreamError(String),
}

pub struct CaptureHandle {
    pub frames: CaptureFrames,
    pub events: Receiver<CaptureEvent>,
    _source: CaptureSource,
}

pub const CAPTURE_FRAME_QUEUE_CAPACITY: usize = 100;

pub struct CaptureFrames {
    queue: Arc<ArrayQueue<Vec<i16>>>,
}

#[derive(Clone)]
pub struct CaptureFrameSender {
    queue: Arc<ArrayQueue<Vec<i16>>>,
    dropped_oldest: Arc<AtomicU64>,
}

impl CaptureFrames {
    pub fn try_recv(&self) -> Result<Vec<i16>, TryRecvError> {
        self.queue.pop().ok_or(TryRecvError::Empty)
    }

    #[cfg(test)]
    pub fn recv_timeout(
        &self,
        timeout: std::time::Duration,
    ) -> Result<Vec<i16>, crossbeam::channel::RecvTimeoutError> {
        let deadline = std::time::Instant::now() + timeout;
        loop {
            if let Some(frame) = self.queue.pop() {
                return Ok(frame);
            }
            let now = std::time::Instant::now();
            if now >= deadline {
                return Err(crossbeam::channel::RecvTimeoutError::Timeout);
            }
            std::thread::park_timeout((deadline - now).min(std::time::Duration::from_millis(1)));
        }
    }
}

impl CaptureFrameSender {
    pub fn send_latest(&self, mut frame: Vec<i16>) -> u64 {
        let mut dropped = 0;
        loop {
            match self.queue.push(frame) {
                Ok(()) => return dropped,
                Err(returned) => {
                    frame = returned;
                    if self.queue.pop().is_some() {
                        dropped = self
                            .dropped_oldest
                            .fetch_add(1, Ordering::Relaxed)
                            .saturating_add(1);
                        continue;
                    }
                    std::hint::spin_loop();
                }
            }
        }
    }
}

pub fn capture_frame_queue(capacity: usize) -> (CaptureFrameSender, CaptureFrames) {
    assert!(
        capacity > 0,
        "capture frame queue capacity must be positive"
    );
    let queue = Arc::new(ArrayQueue::new(capacity));
    (
        CaptureFrameSender {
            queue: Arc::clone(&queue),
            dropped_oldest: Arc::new(AtomicU64::new(0)),
        },
        CaptureFrames { queue },
    )
}

pub enum CaptureSource {
    Cpal { _stream: Stream },
    Thread { _thread: CaptureThread },
}

pub struct CaptureThread {
    stop: Arc<AtomicBool>,
    join: Option<JoinHandle<()>>,
}

impl CaptureThread {
    pub fn new(stop: Arc<AtomicBool>, join: JoinHandle<()>) -> Self {
        Self {
            stop,
            join: Some(join),
        }
    }
}

impl Drop for CaptureThread {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(join) = self.join.take() {
            let _ = join.join();
        }
    }
}

impl CaptureHandle {
    pub fn from_thread(
        frames: CaptureFrames,
        events: Receiver<CaptureEvent>,
        thread: CaptureThread,
    ) -> Self {
        Self {
            frames,
            events,
            _source: CaptureSource::Thread { _thread: thread },
        }
    }
}

pub fn start_capture(audio: &AudioConfig) -> Result<CaptureHandle> {
    let device = find_input_device(audio.mic_device_name.as_deref())?;
    let device_name = device.name().unwrap_or_else(|_| "<unknown>".to_string());
    let supported = device
        .default_input_config()
        .context("获取默认输入配置失败")?;

    tracing::info!(
        "[AUDIO] 输入设备: {} / sample_format={:?} / rate={} / channels={}",
        device_name,
        supported.sample_format(),
        supported.sample_rate().0,
        supported.channels()
    );

    let config = StreamConfig {
        channels: supported.channels(),
        sample_rate: supported.sample_rate(),
        buffer_size: cpal::BufferSize::Default,
    };

    let (frame_tx, frame_rx) = capture_frame_queue(CAPTURE_FRAME_QUEUE_CAPACITY);
    let (event_tx, event_rx) = bounded(16);
    let frame_size = ((audio.sample_rate as usize) * (audio.frame_duration_ms as usize)) / 1000;

    let stream = match supported.sample_format() {
        SampleFormat::F32 => {
            build_input_stream::<f32>(&device, &config, audio, frame_size, frame_tx, event_tx)?
        }
        SampleFormat::I16 => {
            build_input_stream::<i16>(&device, &config, audio, frame_size, frame_tx, event_tx)?
        }
        SampleFormat::U16 => {
            build_input_stream::<u16>(&device, &config, audio, frame_size, frame_tx, event_tx)?
        }
        other => anyhow::bail!("不支持的输入采样格式: {other:?}"),
    };

    stream.play().context("启动输入流失败")?;
    Ok(CaptureHandle {
        frames: frame_rx,
        events: event_rx,
        _source: CaptureSource::Cpal { _stream: stream },
    })
}

fn build_input_stream<T>(
    device: &cpal::Device,
    config: &StreamConfig,
    audio: &AudioConfig,
    frame_size: usize,
    frame_tx: CaptureFrameSender,
    event_tx: Sender<CaptureEvent>,
) -> Result<Stream>
where
    T: cpal::SizedSample,
    f32: cpal::FromSample<T>,
{
    let input_channels = config.channels as usize;
    let input_rate = config.sample_rate.0;
    let needs_resample = input_rate != audio.sample_rate || input_channels != 1;

    let mut raw_buffer: Vec<f32> = Vec::new();
    let mut mono_buffer: Vec<f32> = Vec::new();
    let mut direct_output: Vec<f32> = Vec::new();
    let mut resampler = if needs_resample {
        Some(InputResampler::new(
            input_rate,
            audio.sample_rate,
            input_channels,
        )?)
    } else {
        None
    };

    let event_tx_err = event_tx.clone();
    let stream = device.build_input_stream(
        config,
        move |data: &[T], _: &cpal::InputCallbackInfo| {
            if let Err(err) = process_input_callback(
                data,
                input_channels,
                frame_size,
                &frame_tx,
                &event_tx,
                &mut raw_buffer,
                &mut mono_buffer,
                &mut direct_output,
                resampler.as_mut(),
            ) {
                let _ = event_tx.send(CaptureEvent::StreamError(err.to_string()));
            }
        },
        move |err| {
            let _ = event_tx_err.try_send(CaptureEvent::StreamError(err.to_string()));
        },
        None,
    )?;

    Ok(stream)
}

#[allow(clippy::too_many_arguments)]
fn process_input_callback<T>(
    data: &[T],
    input_channels: usize,
    frame_size: usize,
    frame_tx: &CaptureFrameSender,
    _event_tx: &Sender<CaptureEvent>,
    raw_buffer: &mut Vec<f32>,
    mono_buffer: &mut Vec<f32>,
    direct_output: &mut Vec<f32>,
    mut resampler: Option<&mut InputResampler>,
) -> Result<()>
where
    T: cpal::SizedSample,
    f32: cpal::FromSample<T>,
{
    for sample in data {
        raw_buffer.push(f32::from_sample(*sample));
    }

    if let Some(resampler) = resampler.as_mut() {
        let needed = resampler.input_chunk_frames() * input_channels;
        while raw_buffer.len() >= needed {
            let chunk: Vec<f32> = raw_buffer.drain(..needed).collect();
            let resampled = resampler.process_interleaved(&chunk)?;
            resampler.push_output(&resampled);
            for frame in resampler.drain_frames(frame_size) {
                frame_tx.send_latest(frame);
            }
        }
        return Ok(());
    }

    mono_buffer.append(raw_buffer);
    while mono_buffer.len() >= frame_size {
        direct_output.clear();
        direct_output.extend(mono_buffer.drain(..frame_size));
        let frame: Vec<i16> = direct_output
            .iter()
            .map(|sample| (*sample * 32767.0).clamp(-32768.0, 32767.0) as i16)
            .collect();
        frame_tx.send_latest(frame);
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capture_queue_overwrites_oldest_without_stealing_from_receiver() {
        let (sender, frames) = capture_frame_queue(3);
        assert_eq!(sender.send_latest(vec![1]), 0);
        assert_eq!(sender.send_latest(vec![2]), 0);
        assert_eq!(sender.send_latest(vec![3]), 0);
        assert_eq!(sender.send_latest(vec![4]), 1);

        assert_eq!(frames.try_recv().expect("second frame"), vec![2]);
        assert_eq!(frames.try_recv().expect("third frame"), vec![3]);
        assert_eq!(frames.try_recv().expect("latest frame"), vec![4]);
        assert!(matches!(frames.try_recv(), Err(TryRecvError::Empty)));
    }
}
