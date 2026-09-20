use crate::{
    audio::capture::{
        capture_frame_queue, CaptureEvent, CaptureFrameSender, CaptureHandle, CaptureThread,
        CAPTURE_FRAME_QUEUE_CAPACITY,
    },
    audio_frontend::{
        protocol::{
            decode_s16le_payload, downsample_48k_to_16k, ensure_internal_audio_shape,
            AudioFrontendHeader, FRAME_TYPE_MIC, HEADER_BYTES, WIRE_PAYLOAD_BYTES,
        },
        tcp::{
            connect_with_timeout, wait_interruptibly, TCP_CONNECT_TIMEOUT, TCP_RECONNECT_INTERVAL,
        },
    },
    config::AudioConfig,
};
use anyhow::{Context, Result};
use crossbeam::channel::bounded;
use std::{
    io::{self, Read},
    net::TcpStream,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};

pub fn start_capture_tcp(audio: &AudioConfig) -> Result<CaptureHandle> {
    ensure_internal_audio_shape(audio.sample_rate, audio.frame_duration_ms)?;

    let address = format!("{}:{}", audio.frontend_host, audio.frontend_mic_port);
    let (frame_tx, frame_rx) = capture_frame_queue(CAPTURE_FRAME_QUEUE_CAPACITY);
    let (event_tx, event_rx) = bounded(16);
    let stop = Arc::new(AtomicBool::new(false));
    let stop_for_thread = Arc::clone(&stop);

    let join = std::thread::spawn(move || {
        capture_supervisor(address, frame_tx, event_tx, stop_for_thread);
        tracing::info!("[AUDIO_FRONTEND] mic TCP capture stopped");
    });

    Ok(CaptureHandle::from_thread(
        frame_rx,
        event_rx,
        CaptureThread::new(stop, join),
    ))
}

fn capture_supervisor(
    address: String,
    frame_tx: CaptureFrameSender,
    event_tx: crossbeam::channel::Sender<CaptureEvent>,
    stop: Arc<AtomicBool>,
) {
    let mut outage_notified = false;
    let mut failed_attempts = 0u64;
    while !stop.load(Ordering::Acquire) {
        match connect_capture_stream(&address) {
            Ok(mut stream) => {
                tracing::info!(address = %address, "[AUDIO_FRONTEND] mic TCP capture connected");
                failed_attempts = 0;
                if let Err(err) = capture_loop(&mut stream, &frame_tx, &stop) {
                    if stop.load(Ordering::Acquire) {
                        break;
                    }
                    tracing::warn!(address = %address, error = %err, "[AUDIO_FRONTEND] mic TCP disconnected, retrying");
                    let _ = event_tx.try_send(CaptureEvent::StreamError(format!(
                        "audio_frontend mic TCP interrupted, retrying: {err}"
                    )));
                    outage_notified = true;
                }
            }
            Err(err) => {
                failed_attempts = failed_attempts.saturating_add(1);
                if !outage_notified {
                    let _ = event_tx.try_send(CaptureEvent::StreamError(format!(
                        "audio_frontend mic TCP unavailable, retrying: {err}"
                    )));
                    outage_notified = true;
                }
                if failed_attempts == 1 || failed_attempts % 20 == 0 {
                    tracing::warn!(address = %address, failed_attempts, error = %err, "[AUDIO_FRONTEND] mic TCP connect failed, retrying");
                }
            }
        }
        wait_interruptibly(&stop, TCP_RECONNECT_INTERVAL);
    }
}

fn connect_capture_stream(address: &str) -> Result<TcpStream> {
    let stream = connect_with_timeout(address, TCP_CONNECT_TIMEOUT)
        .with_context(|| format!("连接 audio_frontend mic TCP 失败: {address}"))?;
    stream
        .set_nodelay(true)
        .context("设置 audio_frontend mic TCP_NODELAY 失败")?;
    stream
        .set_read_timeout(Some(Duration::from_millis(200)))
        .context("设置 audio_frontend mic read timeout 失败")?;
    Ok(stream)
}

fn capture_loop(
    stream: &mut TcpStream,
    frame_tx: &CaptureFrameSender,
    stop: &AtomicBool,
) -> Result<()> {
    let mut header_bytes = [0u8; HEADER_BYTES];
    let mut payload = [0u8; WIRE_PAYLOAD_BYTES];
    let mut dropped_frames = 0u64;

    while !stop.load(Ordering::Relaxed) {
        read_exact_interruptible(stream, &mut header_bytes, stop)
            .context("读取 audio_frontend mic header 失败")?;
        let header = AudioFrontendHeader::decode(&header_bytes);
        header
            .validate(FRAME_TYPE_MIC)
            .context("校验 audio_frontend mic header 失败")?;
        read_exact_interruptible(stream, &mut payload, stop)
            .context("读取 audio_frontend mic payload 失败")?;

        let wire_samples = decode_s16le_payload(&payload);
        let frame = downsample_48k_to_16k(&wire_samples);
        let dropped_total = frame_tx.send_latest(frame);
        if dropped_total > dropped_frames {
            dropped_frames = dropped_total;
            if dropped_frames == 1 || dropped_frames % 100 == 0 {
                tracing::warn!(
                    dropped_frames,
                    seq = header.seq,
                    "[AUDIO_FRONTEND] mic TCP capture queue full, dropped oldest frame"
                );
            }
        }
    }

    Ok(())
}

fn read_exact_interruptible(
    stream: &mut TcpStream,
    mut destination: &mut [u8],
    stop: &AtomicBool,
) -> io::Result<()> {
    while !destination.is_empty() {
        if stop.load(Ordering::Relaxed) {
            return Ok(());
        }
        match stream.read(destination) {
            Ok(0) => {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "audio_frontend TCP closed",
                ));
            }
            Ok(read) => {
                let (_, rest) = destination.split_at_mut(read);
                destination = rest;
            }
            Err(err)
                if matches!(
                    err.kind(),
                    io::ErrorKind::WouldBlock
                        | io::ErrorKind::TimedOut
                        | io::ErrorKind::Interrupted
                ) =>
            {
                continue;
            }
            Err(err) => return Err(err),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        audio_frontend::protocol::{
            FORMAT_S16LE, MAGIC, VERSION, WIRE_CHANNELS, WIRE_FRAME_MS, WIRE_SAMPLES_PER_CHANNEL,
            WIRE_SAMPLE_RATE,
        },
        config::AudioFrontendMode,
    };
    use std::{
        io::Write,
        net::TcpListener,
        thread,
        time::{Duration, SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn tcp_capture_emits_16k_frames() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("local addr").port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept client");
            stream.write_all(&mic_header(7)).expect("write header");
            stream.write_all(&mic_payload()).expect("write payload");
        });

        let audio = AudioConfig {
            sample_rate: 16_000,
            channels: 1,
            frame_duration_ms: 10,
            tts_source_rate: 16_000,
            playback_buffer_frames: 2048,
            frontend_mode: AudioFrontendMode::Tcp,
            frontend_host: "127.0.0.1".to_string(),
            frontend_mic_port: port,
            frontend_speaker_port: 39_002,
            frontend_control_port: 39_003,
            frontend_mute_mic_during_playback: false,
            tts_barge_in_with_aec: true,
            natural_barge_in_enabled: false,
            frontend_upsampler: crate::config::AudioFrontendUpsampler::LinearLookahead,
            mic_device_name: None,
            speaker_device_name: None,
            pause_capture_while_waiting: true,
            wake_capture_prebuffer_ms: 800,
        };

        let capture = start_capture_tcp(&audio).expect("start tcp capture");
        let frame = capture
            .frames
            .recv_timeout(Duration::from_secs(2))
            .expect("receive one frame");

        assert_eq!(frame.len(), 160);
        assert_eq!(frame[0], 1);
        assert_eq!(frame[1], 4);
        drop(capture);
        server.join().expect("server joins");
    }

    #[test]
    fn tcp_capture_reconnects_after_peer_restart() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        let port = listener.local_addr().expect("local addr").port();
        let server = thread::spawn(move || {
            for seq in [7, 8] {
                let (mut stream, _) = listener.accept().expect("accept client");
                stream.write_all(&mic_header(seq)).expect("write header");
                stream.write_all(&mic_payload()).expect("write payload");
            }
        });

        let audio = test_audio_config(port);
        let capture = start_capture_tcp(&audio).expect("start tcp capture");
        let first = capture
            .frames
            .recv_timeout(Duration::from_secs(2))
            .expect("receive first frame");
        assert_eq!(first[0], 1);
        assert!(matches!(
            capture.events.recv_timeout(Duration::from_secs(2)),
            Ok(CaptureEvent::StreamError(_))
        ));
        let second = capture
            .frames
            .recv_timeout(Duration::from_secs(3))
            .expect("receive frame after reconnect");
        assert_eq!(second[1], 4);

        drop(capture);
        server.join().expect("server joins");
    }

    fn test_audio_config(port: u16) -> AudioConfig {
        AudioConfig {
            sample_rate: 16_000,
            channels: 1,
            frame_duration_ms: 10,
            tts_source_rate: 16_000,
            playback_buffer_frames: 2048,
            frontend_mode: AudioFrontendMode::Tcp,
            frontend_host: "127.0.0.1".to_string(),
            frontend_mic_port: port,
            frontend_speaker_port: 39_002,
            frontend_control_port: 39_003,
            frontend_mute_mic_during_playback: false,
            tts_barge_in_with_aec: true,
            natural_barge_in_enabled: false,
            frontend_upsampler: crate::config::AudioFrontendUpsampler::LinearLookahead,
            mic_device_name: None,
            speaker_device_name: None,
            pause_capture_while_waiting: true,
            wake_capture_prebuffer_ms: 800,
        }
    }

    fn mic_header(seq: u64) -> [u8; HEADER_BYTES] {
        let mut bytes = [0u8; HEADER_BYTES];
        bytes[0..4].copy_from_slice(&MAGIC.to_le_bytes());
        bytes[4..6].copy_from_slice(&VERSION.to_le_bytes());
        bytes[6..8].copy_from_slice(&(HEADER_BYTES as u16).to_le_bytes());
        bytes[8..10].copy_from_slice(&FRAME_TYPE_MIC.to_le_bytes());
        bytes[10..12].copy_from_slice(&FORMAT_S16LE.to_le_bytes());
        bytes[12..16].copy_from_slice(&WIRE_SAMPLE_RATE.to_le_bytes());
        bytes[16..18].copy_from_slice(&WIRE_CHANNELS.to_le_bytes());
        bytes[18..20].copy_from_slice(&WIRE_FRAME_MS.to_le_bytes());
        bytes[20..24].copy_from_slice(&(WIRE_SAMPLES_PER_CHANNEL as u32).to_le_bytes());
        bytes[24..32].copy_from_slice(&seq.to_le_bytes());
        bytes[32..40].copy_from_slice(&monotonicish_us().to_le_bytes());
        bytes[40..44].copy_from_slice(&(WIRE_PAYLOAD_BYTES as u32).to_le_bytes());
        bytes
    }

    fn mic_payload() -> [u8; WIRE_PAYLOAD_BYTES] {
        let mut payload = [0u8; WIRE_PAYLOAD_BYTES];
        for sample_index in 0..WIRE_SAMPLES_PER_CHANNEL {
            let value = sample_index as i16;
            let offset = sample_index * 2;
            payload[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
        }
        payload
    }

    fn monotonicish_us() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_micros() as u64)
            .unwrap_or(0)
    }
}
