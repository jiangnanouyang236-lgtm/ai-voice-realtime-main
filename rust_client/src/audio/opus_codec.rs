use anyhow::{ensure, Context, Result};
use opus::{Application, Bitrate, Channels, Decoder, Encoder};
use std::env;

pub const OPUS_PACKET_STREAM_MAGIC: &[u8; 8] = b"OPUSRAW1";
pub const OPUS_FRAME_DURATION_MS: u16 = 20;
pub const OPUS_SAMPLE_RATE: u32 = 16_000;
pub const OPUS_CHANNELS: u16 = 1;
pub const DEFAULT_OPUS_BITRATE_BPS: i32 = 16_000;

const MAX_OPUS_PACKET_BYTES: usize = 4000;
const MIN_OPUS_BITRATE_BPS: i32 = 6_000;
const MAX_OPUS_BITRATE_BPS: i32 = 64_000;

#[derive(Debug, Clone)]
pub struct OpusPacketStream {
    pub payload: Vec<u8>,
    pub packet_count: u32,
    pub frame_duration_ms: u16,
}

pub struct OpusStreamDecoder {
    decoder: Decoder,
    sample_rate: u32,
    channels: u16,
    frame_duration_ms: u16,
}

impl OpusStreamDecoder {
    pub fn new(sample_rate: u32, channels: u16, frame_duration_ms: u16) -> Result<Self> {
        validate_opus_decode_config(sample_rate, channels, frame_duration_ms)?;
        let decoder = Decoder::new(sample_rate, Channels::Mono).context("创建 Opus 解码器失败")?;
        Ok(Self {
            decoder,
            sample_rate,
            channels,
            frame_duration_ms,
        })
    }

    pub fn reset(&mut self) -> Result<()> {
        self.decoder.reset_state().context("重置 Opus 解码器失败")
    }

    pub fn decode_packet_stream(
        &mut self,
        payload: &[u8],
        duration_ms: Option<f64>,
    ) -> Result<Vec<i16>> {
        let packets = parse_opus_packet_stream(payload)?;
        let frame_samples = (self.sample_rate as usize * self.frame_duration_ms as usize / 1000)
            * self.channels as usize;
        ensure!(frame_samples > 0, "Opus 帧采样数非法");

        let mut samples = Vec::with_capacity(packets.len() * frame_samples);
        for packet in packets {
            let mut decoded = vec![0i16; frame_samples];
            let decoded_per_channel = self
                .decoder
                .decode(packet, &mut decoded, false)
                .context("Opus 解码失败")?;
            let decoded_samples = decoded_per_channel * self.channels as usize;
            samples.extend_from_slice(&decoded[..decoded_samples.min(decoded.len())]);
        }

        truncate_to_duration(&mut samples, self.sample_rate, self.channels, duration_ms);
        ensure!(!samples.is_empty(), "Opus 解码后音频为空");
        Ok(samples)
    }
}

pub fn configured_opus_bitrate_bps() -> i32 {
    env::var("OPUS_BITRATE_BPS")
        .ok()
        .and_then(|value| value.trim().parse::<i32>().ok())
        .unwrap_or(DEFAULT_OPUS_BITRATE_BPS)
        .clamp(MIN_OPUS_BITRATE_BPS, MAX_OPUS_BITRATE_BPS)
}

pub fn encode_pcm16_opus_packet_stream(
    samples: &[i16],
    sample_rate: u32,
    channels: u16,
) -> Result<OpusPacketStream> {
    ensure!(!samples.is_empty(), "Opus 上行音频不能为空");
    ensure!(
        sample_rate == OPUS_SAMPLE_RATE,
        "Opus 上行仅支持 {OPUS_SAMPLE_RATE}Hz，当前为 {sample_rate}Hz"
    );
    ensure!(
        channels == OPUS_CHANNELS,
        "Opus 上行仅支持单声道，当前为 {channels} 声道"
    );

    let frame_samples =
        (sample_rate as usize * OPUS_FRAME_DURATION_MS as usize / 1000) * channels as usize;
    ensure!(frame_samples > 0, "Opus 帧采样数非法");

    let mut encoder = Encoder::new(sample_rate, Channels::Mono, Application::Voip)
        .context("创建 Opus 编码器失败")?;
    encoder
        .set_bitrate(Bitrate::Bits(configured_opus_bitrate_bps()))
        .context("配置 Opus 码率失败")?;
    encoder.set_vbr(true).context("配置 Opus VBR 失败")?;
    encoder
        .set_complexity(5)
        .context("配置 Opus 编码复杂度失败")?;

    let mut payload = Vec::with_capacity(OPUS_PACKET_STREAM_MAGIC.len() + samples.len());
    payload.extend_from_slice(OPUS_PACKET_STREAM_MAGIC);

    let mut packet_count = 0u32;
    for chunk in samples.chunks(frame_samples) {
        let mut frame = vec![0i16; frame_samples];
        frame[..chunk.len()].copy_from_slice(chunk);

        let packet = encoder
            .encode_vec(&frame, MAX_OPUS_PACKET_BYTES)
            .context("Opus 编码失败")?;
        let packet_len = u16::try_from(packet.len()).context("Opus packet 过大")?;
        ensure!(packet_len > 0, "Opus packet 为空");

        payload.extend_from_slice(&packet_len.to_be_bytes());
        payload.extend_from_slice(&packet);
        packet_count += 1;
    }

    Ok(OpusPacketStream {
        payload,
        packet_count,
        frame_duration_ms: OPUS_FRAME_DURATION_MS,
    })
}

#[cfg(test)]
pub fn decode_opus_packet_stream(
    payload: &[u8],
    sample_rate: u32,
    channels: u16,
    frame_duration_ms: u16,
    duration_ms: Option<f64>,
) -> Result<Vec<i16>> {
    let mut decoder = OpusStreamDecoder::new(sample_rate, channels, frame_duration_ms)?;
    decoder.decode_packet_stream(payload, duration_ms)
}

pub fn parse_opus_packet_stream(payload: &[u8]) -> Result<Vec<&[u8]>> {
    ensure!(
        payload.starts_with(OPUS_PACKET_STREAM_MAGIC),
        "Opus payload magic 不匹配"
    );
    let mut packets = Vec::new();
    let mut offset = OPUS_PACKET_STREAM_MAGIC.len();
    while offset < payload.len() {
        ensure!(offset + 2 <= payload.len(), "Opus packet 长度字段不完整");
        let packet_len = u16::from_be_bytes([payload[offset], payload[offset + 1]]) as usize;
        offset += 2;
        ensure!(packet_len > 0, "Opus packet 为空");
        let packet_end = offset
            .checked_add(packet_len)
            .context("Opus packet 长度溢出")?;
        ensure!(packet_end <= payload.len(), "Opus packet 数据不完整");
        packets.push(&payload[offset..packet_end]);
        offset = packet_end;
    }
    ensure!(!packets.is_empty(), "Opus payload 不包含音频 packet");
    Ok(packets)
}

fn validate_opus_decode_config(
    sample_rate: u32,
    channels: u16,
    frame_duration_ms: u16,
) -> Result<()> {
    ensure!(
        sample_rate == OPUS_SAMPLE_RATE,
        "Opus 下行仅支持 {OPUS_SAMPLE_RATE}Hz，当前为 {sample_rate}Hz"
    );
    ensure!(
        channels == OPUS_CHANNELS,
        "Opus 下行仅支持单声道，当前为 {channels} 声道"
    );
    ensure!(
        frame_duration_ms == OPUS_FRAME_DURATION_MS,
        "Opus 下行仅支持 {OPUS_FRAME_DURATION_MS}ms frame，当前为 {frame_duration_ms}ms"
    );
    Ok(())
}

fn truncate_to_duration(
    samples: &mut Vec<i16>,
    sample_rate: u32,
    channels: u16,
    duration_ms: Option<f64>,
) {
    let Some(duration_ms) = duration_ms else {
        return;
    };
    if duration_ms <= 0.0 {
        return;
    }
    let expected_samples =
        (sample_rate as f64 * duration_ms / 1000.0).round() as usize * channels as usize;
    if expected_samples > 0 && expected_samples <= samples.len() {
        samples.truncate(expected_samples);
    }
}

#[cfg(test)]
mod tests {
    use super::{
        decode_opus_packet_stream, encode_pcm16_opus_packet_stream, parse_opus_packet_stream,
        OpusStreamDecoder, OPUS_CHANNELS, OPUS_FRAME_DURATION_MS, OPUS_PACKET_STREAM_MAGIC,
        OPUS_SAMPLE_RATE,
    };

    #[test]
    fn opus_packet_stream_round_trips_one_frame() {
        let samples =
            vec![0i16; OPUS_SAMPLE_RATE as usize * OPUS_FRAME_DURATION_MS as usize / 1000];
        let encoded =
            encode_pcm16_opus_packet_stream(&samples, OPUS_SAMPLE_RATE, OPUS_CHANNELS).unwrap();

        let decoded = decode_opus_packet_stream(
            &encoded.payload,
            OPUS_SAMPLE_RATE,
            OPUS_CHANNELS,
            OPUS_FRAME_DURATION_MS,
            Some(OPUS_FRAME_DURATION_MS as f64),
        )
        .unwrap();

        assert_eq!(samples.len(), decoded.len());
        assert_eq!(1, encoded.packet_count);
    }

    #[test]
    fn opus_stream_decoder_keeps_state_across_single_packet_payloads() {
        let frame_samples = OPUS_SAMPLE_RATE as usize * OPUS_FRAME_DURATION_MS as usize / 1000;
        let samples = vec![0i16; frame_samples * 2];
        let encoded =
            encode_pcm16_opus_packet_stream(&samples, OPUS_SAMPLE_RATE, OPUS_CHANNELS).unwrap();
        let packets = parse_opus_packet_stream(&encoded.payload).unwrap();
        assert_eq!(2, packets.len());

        let first_payload = single_packet_payload(packets[0]);
        let second_payload = single_packet_payload(packets[1]);
        let mut decoder =
            OpusStreamDecoder::new(OPUS_SAMPLE_RATE, OPUS_CHANNELS, OPUS_FRAME_DURATION_MS)
                .unwrap();

        let first = decoder
            .decode_packet_stream(&first_payload, Some(OPUS_FRAME_DURATION_MS as f64))
            .unwrap();
        let second = decoder
            .decode_packet_stream(&second_payload, Some(OPUS_FRAME_DURATION_MS as f64))
            .unwrap();

        assert_eq!(frame_samples, first.len());
        assert_eq!(frame_samples, second.len());
        decoder.reset().unwrap();
        let first_after_reset = decoder
            .decode_packet_stream(&first_payload, Some(OPUS_FRAME_DURATION_MS as f64))
            .unwrap();
        assert_eq!(frame_samples, first_after_reset.len());
    }

    fn single_packet_payload(packet: &[u8]) -> Vec<u8> {
        let mut payload = Vec::with_capacity(OPUS_PACKET_STREAM_MAGIC.len() + 2 + packet.len());
        payload.extend_from_slice(OPUS_PACKET_STREAM_MAGIC);
        payload.extend_from_slice(&(packet.len() as u16).to_be_bytes());
        payload.extend_from_slice(packet);
        payload
    }
}
