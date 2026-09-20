use crate::{
    audio::{
        playback::{PlaybackCommand, PlaybackGeneration},
        resample::resample_linear_i16,
    },
    tts::prebuffer::TtsPrebuffer,
};
use crossbeam::channel::{Sender as CrossbeamSender, TrySendError};
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::{info, warn};

#[derive(Debug)]
pub enum TtsProcessorCommand {
    StartRound,
    Audio(Vec<u8>),
    Finish,
    Reset,
}

#[derive(Debug)]
struct TaggedTtsProcessorCommand {
    generation: u64,
    command: TtsProcessorCommand,
}

#[derive(Clone)]
pub struct TtsProcessorHandle {
    tx: mpsc::Sender<TaggedTtsProcessorCommand>,
    generation: PlaybackGeneration,
}

impl TtsProcessorHandle {
    pub fn try_send(&self, command: TtsProcessorCommand) -> Result<(), String> {
        self.tx
            .try_send(TaggedTtsProcessorCommand {
                generation: self.generation.current(),
                command,
            })
            .map_err(|err| err.to_string())
    }
}

#[derive(Debug)]
pub enum TtsProcessorEvent {
    PlaybackStarted { generation: u64 },
}

#[derive(Debug, Default)]
struct TtsProcessorStats {
    received_chunks: u64,
    received_bytes: u64,
    decoded_samples: u64,
    empty_chunks: u64,
    odd_tail_bytes: u64,
    playback_chunks_sent: u64,
    playback_samples_sent: u64,
    playback_chunks_dropped: u64,
    playback_samples_dropped: u64,
}

impl TtsProcessorStats {
    fn has_activity(&self) -> bool {
        self.received_chunks > 0
            || self.received_bytes > 0
            || self.playback_chunks_sent > 0
            || self.playback_chunks_dropped > 0
    }
}

pub fn spawn_tts_processor(
    sample_rate: u32,
    tts_source_rate: u32,
    prebuffer_target_samples: usize,
    playback_tx: CrossbeamSender<PlaybackCommand>,
    generation: PlaybackGeneration,
) -> (TtsProcessorHandle, mpsc::Receiver<TtsProcessorEvent>) {
    let (cmd_tx, mut cmd_rx) = mpsc::channel::<TaggedTtsProcessorCommand>(256);
    let (event_tx, event_rx) = mpsc::channel::<TtsProcessorEvent>(64);
    let processor_generation = generation.clone();

    tokio::spawn(async move {
        let mut prebuffer = TtsPrebuffer::new(prebuffer_target_samples);
        let mut stats = TtsProcessorStats::default();
        let mut observed_generation = processor_generation.current();

        while let Some(tagged) = cmd_rx.recv().await {
            let current_generation = processor_generation.current();
            sync_processor_generation(
                &mut prebuffer,
                &mut stats,
                &mut observed_generation,
                current_generation,
            );
            let command_generation = tagged.generation;
            match tagged.command {
                TtsProcessorCommand::StartRound
                    if processor_generation.is_current(command_generation) =>
                {
                    prebuffer.clear();
                    stats = TtsProcessorStats::default();
                }
                TtsProcessorCommand::Audio(bytes)
                    if processor_generation.is_current(command_generation) =>
                {
                    stats.received_chunks += 1;
                    stats.received_bytes += bytes.len() as u64;
                    let playback_started = prebuffer.is_playback_started();
                    let mut chunks = bytes.chunks_exact(2);
                    let samples: Vec<i16> = chunks
                        .by_ref()
                        .map(|b| i16::from_le_bytes([b[0], b[1]]))
                        .collect();
                    if !chunks.remainder().is_empty() {
                        stats.odd_tail_bytes += chunks.remainder().len() as u64;
                        warn!("[TTS][WARN] PCM16LE 音频块长度不是 2 的倍数，已忽略尾字节");
                    }
                    if samples.is_empty() {
                        stats.empty_chunks += 1;
                        warn!("[TTS][WARN] 收到空音频块，已忽略");
                        continue;
                    }
                    stats.decoded_samples += samples.len() as u64;

                    let resampled = resample_linear_i16(&samples, tts_source_rate, sample_rate);
                    if !processor_generation.is_current(command_generation) {
                        prebuffer.clear();
                        stats = TtsProcessorStats::default();
                        continue;
                    }

                    if let Some(chunks) = prebuffer.push(resampled) {
                        if !playback_started && prebuffer.is_playback_started() {
                            let _ = event_tx
                                .send(TtsProcessorEvent::PlaybackStarted {
                                    generation: command_generation,
                                })
                                .await;
                        }
                        for chunk in chunks {
                            send_playback_chunk(
                                &playback_tx,
                                &processor_generation,
                                command_generation,
                                &mut stats,
                                chunk,
                            );
                        }
                    }
                }
                TtsProcessorCommand::Finish
                    if processor_generation.is_current(command_generation) =>
                {
                    let remaining = prebuffer.flush_all();
                    for chunk in remaining {
                        send_playback_chunk(
                            &playback_tx,
                            &processor_generation,
                            command_generation,
                            &mut stats,
                            chunk,
                        );
                    }
                    if !processor_generation.is_current(command_generation) {
                        prebuffer.clear();
                        stats = TtsProcessorStats::default();
                        continue;
                    }
                    send_mark_done(&playback_tx, &processor_generation, command_generation).await;
                    log_tts_processor_stats(&stats);
                }
                TtsProcessorCommand::Reset
                    if processor_generation.is_current(command_generation) =>
                {
                    prebuffer.clear();
                    stats = TtsProcessorStats::default();
                }
                _ => {}
            }
        }
    });

    (
        TtsProcessorHandle {
            tx: cmd_tx,
            generation,
        },
        event_rx,
    )
}

async fn send_mark_done(
    playback_tx: &CrossbeamSender<PlaybackCommand>,
    generation: &PlaybackGeneration,
    command_generation: u64,
) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    loop {
        if !generation.is_current(command_generation) {
            return;
        }
        match playback_tx.try_send(PlaybackCommand::MarkDone {
            generation: command_generation,
        }) {
            Ok(()) => return,
            Err(TrySendError::Full(_)) if tokio::time::Instant::now() < deadline => {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
            Err(err) => {
                warn!("[TTS][WARN] 播放完成标记提交失败: {}", err);
                return;
            }
        }
    }
}

fn sync_processor_generation(
    prebuffer: &mut TtsPrebuffer,
    stats: &mut TtsProcessorStats,
    observed_generation: &mut u64,
    current_generation: u64,
) {
    if *observed_generation == current_generation {
        return;
    }
    prebuffer.clear();
    *stats = TtsProcessorStats::default();
    *observed_generation = current_generation;
}

fn send_playback_chunk(
    playback_tx: &CrossbeamSender<PlaybackCommand>,
    generation: &PlaybackGeneration,
    command_generation: u64,
    stats: &mut TtsProcessorStats,
    chunk: Vec<i16>,
) {
    if !generation.is_current(command_generation) {
        return;
    }
    let samples = chunk.len() as u64;
    match playback_tx.try_send(PlaybackCommand::PlayPcm16k {
        generation: command_generation,
        samples: chunk,
    }) {
        Ok(()) => {
            stats.playback_chunks_sent += 1;
            stats.playback_samples_sent += samples;
        }
        Err(err) => {
            stats.playback_chunks_dropped += 1;
            stats.playback_samples_dropped += samples;
            warn!(
                samples,
                "[TTS][WARN] 播放队列提交失败，音频块已丢弃: {}", err
            );
        }
    }
}

fn log_tts_processor_stats(stats: &TtsProcessorStats) {
    if !stats.has_activity() {
        return;
    }
    info!(
        recv_chunks = stats.received_chunks,
        recv_bytes = stats.received_bytes,
        decoded_samples = stats.decoded_samples,
        playback_chunks = stats.playback_chunks_sent,
        playback_samples = stats.playback_samples_sent,
        dropped_chunks = stats.playback_chunks_dropped,
        dropped_samples = stats.playback_samples_dropped,
        empty_chunks = stats.empty_chunks,
        odd_tail_bytes = stats.odd_tail_bytes,
        "[TTS] 处理完成"
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossbeam::channel::bounded;

    #[test]
    fn generation_switch_clears_old_prebuffer_without_reset_command() {
        let mut prebuffer = TtsPrebuffer::new(4);
        let mut stats = TtsProcessorStats::default();
        let mut observed_generation = 0;

        assert!(prebuffer.push(vec![11, 11]).is_none());
        sync_processor_generation(&mut prebuffer, &mut stats, &mut observed_generation, 1);
        let chunks = prebuffer
            .push(vec![22, 22, 22, 22])
            .expect("new generation should start independently");

        assert_eq!(chunks, vec![vec![22, 22, 22, 22]]);
        assert_eq!(observed_generation, 1);
    }

    #[tokio::test]
    async fn stale_audio_and_finish_are_rejected_while_new_generation_works() {
        let generation = PlaybackGeneration::default();
        let (playback_tx, playback_rx) = bounded(16);
        let (handle, mut event_rx) =
            spawn_tts_processor(16_000, 16_000, 0, playback_tx, generation.clone());
        let old_generation = generation.current();
        generation.advance();
        let new_generation = generation.current();

        handle
            .tx
            .try_send(TaggedTtsProcessorCommand {
                generation: old_generation,
                command: TtsProcessorCommand::Audio(vec![1, 0]),
            })
            .expect("queue stale audio");
        handle
            .tx
            .try_send(TaggedTtsProcessorCommand {
                generation: old_generation,
                command: TtsProcessorCommand::Finish,
            })
            .expect("queue stale finish");
        handle
            .try_send(TtsProcessorCommand::StartRound)
            .expect("start new generation");
        handle
            .try_send(TtsProcessorCommand::Audio(vec![2, 0]))
            .expect("queue new audio");
        handle
            .try_send(TtsProcessorCommand::Finish)
            .expect("finish new generation");

        let event = tokio::time::timeout(Duration::from_secs(1), event_rx.recv())
            .await
            .expect("playback event timeout")
            .expect("playback event channel closed");
        assert!(matches!(
            event,
            TtsProcessorEvent::PlaybackStarted { generation } if generation == new_generation
        ));

        let mut commands = Vec::new();
        for _ in 0..2 {
            commands.push(
                tokio::time::timeout(Duration::from_secs(1), async {
                    loop {
                        if let Ok(command) = playback_rx.try_recv() {
                            break command;
                        }
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .expect("playback command timeout"),
            );
        }
        assert!(matches!(
            &commands[0],
            PlaybackCommand::PlayPcm16k { generation, samples }
                if *generation == new_generation && samples == &vec![2]
        ));
        assert!(matches!(
            commands[1],
            PlaybackCommand::MarkDone { generation } if generation == new_generation
        ));
        assert!(playback_rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn cancel_unblocks_full_mark_done_queue_and_processes_new_generation_quickly() {
        let generation = PlaybackGeneration::default();
        let old_generation = generation.current();
        let (playback_tx, playback_rx) = bounded(1);
        playback_tx
            .send(PlaybackCommand::PlayFeedbackPcm16k {
                generation: old_generation,
                samples: vec![0],
            })
            .expect("fill playback queue");
        let (handle, _event_rx) =
            spawn_tts_processor(16_000, 16_000, 0, playback_tx, generation.clone());
        handle
            .try_send(TtsProcessorCommand::Finish)
            .expect("queue old finish");

        tokio::time::timeout(Duration::from_millis(100), async {
            while handle.tx.capacity() != handle.tx.max_capacity() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("processor should enter mark-done retry");

        generation.advance();
        playback_rx.try_recv().expect("remove queue filler");
        handle
            .try_send(TtsProcessorCommand::StartRound)
            .expect("start new generation");
        handle
            .try_send(TtsProcessorCommand::Audio(vec![3, 0]))
            .expect("queue new audio");
        handle
            .try_send(TtsProcessorCommand::Finish)
            .expect("finish new generation");
        let new_generation = generation.current();

        let commands = tokio::time::timeout(Duration::from_millis(100), async {
            let mut commands = Vec::new();
            loop {
                if let Ok(command) = playback_rx.try_recv() {
                    let done = matches!(
                        command,
                        PlaybackCommand::MarkDone { generation }
                            if generation == new_generation
                    );
                    commands.push(command);
                    if done {
                        break commands;
                    }
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("new generation should not wait for old 2s mark-done timeout");

        assert!(commands.iter().all(|command| !matches!(
            command,
            PlaybackCommand::MarkDone { generation } if *generation == old_generation
        )));
        assert!(commands.iter().any(|command| matches!(
            command,
            PlaybackCommand::PlayPcm16k { generation, samples }
                if *generation == new_generation && samples == &vec![3]
        )));
    }
}
