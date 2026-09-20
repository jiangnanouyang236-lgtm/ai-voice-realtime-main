use anyhow::{bail, ensure, Result};

pub const MAGIC: u32 = 0x3044_5541;
pub const VERSION: u16 = 1;
pub const HEADER_BYTES: usize = 48;
pub const FRAME_TYPE_MIC: u16 = 1;
pub const FRAME_TYPE_SPEAKER: u16 = 2;
pub const FORMAT_S16LE: u16 = 1;
pub const WIRE_SAMPLE_RATE: u32 = 48_000;
pub const WIRE_CHANNELS: u16 = 1;
pub const WIRE_FRAME_MS: u16 = 10;
pub const WIRE_SAMPLES_PER_CHANNEL: usize = 480;
pub const WIRE_PAYLOAD_BYTES: usize = 960;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AudioFrontendHeader {
    pub magic: u32,
    pub version: u16,
    pub header_bytes: u16,
    pub frame_type: u16,
    pub format: u16,
    pub sample_rate: u32,
    pub channels: u16,
    pub frame_ms: u16,
    pub samples_per_channel: u32,
    pub seq: u64,
    pub timestamp_us: u64,
    pub payload_bytes: u32,
    pub flags: u32,
}

impl AudioFrontendHeader {
    pub fn speaker(seq: u64, timestamp_us: u64) -> Self {
        Self {
            magic: MAGIC,
            version: VERSION,
            header_bytes: HEADER_BYTES as u16,
            frame_type: FRAME_TYPE_SPEAKER,
            format: FORMAT_S16LE,
            sample_rate: WIRE_SAMPLE_RATE,
            channels: WIRE_CHANNELS,
            frame_ms: WIRE_FRAME_MS,
            samples_per_channel: WIRE_SAMPLES_PER_CHANNEL as u32,
            seq,
            timestamp_us,
            payload_bytes: WIRE_PAYLOAD_BYTES as u32,
            flags: 0,
        }
    }

    pub fn decode(bytes: &[u8; HEADER_BYTES]) -> Self {
        Self {
            magic: get_u32(bytes, 0),
            version: get_u16(bytes, 4),
            header_bytes: get_u16(bytes, 6),
            frame_type: get_u16(bytes, 8),
            format: get_u16(bytes, 10),
            sample_rate: get_u32(bytes, 12),
            channels: get_u16(bytes, 16),
            frame_ms: get_u16(bytes, 18),
            samples_per_channel: get_u32(bytes, 20),
            seq: get_u64(bytes, 24),
            timestamp_us: get_u64(bytes, 32),
            payload_bytes: get_u32(bytes, 40),
            flags: get_u32(bytes, 44),
        }
    }

    pub fn encode(&self) -> [u8; HEADER_BYTES] {
        let mut bytes = [0u8; HEADER_BYTES];
        put_u32(&mut bytes, 0, self.magic);
        put_u16(&mut bytes, 4, self.version);
        put_u16(&mut bytes, 6, self.header_bytes);
        put_u16(&mut bytes, 8, self.frame_type);
        put_u16(&mut bytes, 10, self.format);
        put_u32(&mut bytes, 12, self.sample_rate);
        put_u16(&mut bytes, 16, self.channels);
        put_u16(&mut bytes, 18, self.frame_ms);
        put_u32(&mut bytes, 20, self.samples_per_channel);
        put_u64(&mut bytes, 24, self.seq);
        put_u64(&mut bytes, 32, self.timestamp_us);
        put_u32(&mut bytes, 40, self.payload_bytes);
        put_u32(&mut bytes, 44, self.flags);
        bytes
    }

    pub fn validate(&self, expected_type: u16) -> Result<()> {
        ensure!(self.magic == MAGIC, "invalid audio_frontend magic");
        ensure!(
            self.version == VERSION,
            "unsupported audio_frontend protocol version: {}",
            self.version
        );
        ensure!(
            self.header_bytes == HEADER_BYTES as u16,
            "invalid audio_frontend header size: {}",
            self.header_bytes
        );
        ensure!(
            self.frame_type == expected_type,
            "unexpected audio_frontend frame type: {}",
            self.frame_type
        );
        ensure!(
            self.format == FORMAT_S16LE,
            "unsupported audio_frontend format: {}",
            self.format
        );
        ensure!(
            self.sample_rate == WIRE_SAMPLE_RATE,
            "unexpected audio_frontend sample rate: {}",
            self.sample_rate
        );
        ensure!(
            self.channels == WIRE_CHANNELS,
            "unexpected audio_frontend channels: {}",
            self.channels
        );
        ensure!(
            self.frame_ms == WIRE_FRAME_MS,
            "unexpected audio_frontend frame duration: {}",
            self.frame_ms
        );
        ensure!(
            self.samples_per_channel == WIRE_SAMPLES_PER_CHANNEL as u32,
            "unexpected audio_frontend samples_per_channel: {}",
            self.samples_per_channel
        );
        ensure!(
            self.payload_bytes == WIRE_PAYLOAD_BYTES as u32,
            "unexpected audio_frontend payload bytes: {}",
            self.payload_bytes
        );
        Ok(())
    }
}

pub fn decode_s16le_payload(bytes: &[u8; WIRE_PAYLOAD_BYTES]) -> [i16; WIRE_SAMPLES_PER_CHANNEL] {
    let mut samples = [0i16; WIRE_SAMPLES_PER_CHANNEL];
    for (sample, chunk) in samples.iter_mut().zip(bytes.chunks_exact(2)) {
        *sample = i16::from_le_bytes([chunk[0], chunk[1]]);
    }
    samples
}

pub fn encode_s16le_payload(samples: &[i16; WIRE_SAMPLES_PER_CHANNEL]) -> [u8; WIRE_PAYLOAD_BYTES] {
    let mut bytes = [0u8; WIRE_PAYLOAD_BYTES];
    for (sample, chunk) in samples.iter().zip(bytes.chunks_exact_mut(2)) {
        chunk.copy_from_slice(&sample.to_le_bytes());
    }
    bytes
}

pub fn downsample_48k_to_16k(samples: &[i16; WIRE_SAMPLES_PER_CHANNEL]) -> Vec<i16> {
    samples
        .chunks_exact(3)
        .map(|chunk| {
            let sum = chunk[0] as i32 + chunk[1] as i32 + chunk[2] as i32;
            (sum / 3).clamp(i16::MIN as i32, i16::MAX as i32) as i16
        })
        .collect()
}

pub fn upsample_16k_to_48k(samples: &[i16]) -> Result<[i16; WIRE_SAMPLES_PER_CHANNEL]> {
    upsample_16k_to_48k_with_next(samples, None)
}

pub fn repeat_upsample_16k_to_48k(samples: &[i16]) -> Result<[i16; WIRE_SAMPLES_PER_CHANNEL]> {
    ensure!(
        samples.len() == WIRE_SAMPLES_PER_CHANNEL / 3,
        "audio_frontend speaker frame expects 160 samples at 16kHz, got {}",
        samples.len()
    );
    let mut output = [0i16; WIRE_SAMPLES_PER_CHANNEL];
    for (index, sample) in samples.iter().enumerate() {
        let base = index * 3;
        output[base] = *sample;
        output[base + 1] = *sample;
        output[base + 2] = *sample;
    }
    Ok(output)
}

pub fn upsample_16k_to_48k_with_next(
    samples: &[i16],
    next_sample: Option<i16>,
) -> Result<[i16; WIRE_SAMPLES_PER_CHANNEL]> {
    ensure!(
        samples.len() == WIRE_SAMPLES_PER_CHANNEL / 3,
        "audio_frontend speaker frame expects 160 samples at 16kHz, got {}",
        samples.len()
    );
    let mut output = [0i16; WIRE_SAMPLES_PER_CHANNEL];
    for (index, sample) in samples.iter().enumerate() {
        let base = index * 3;
        let current = i32::from(*sample);
        let next = samples
            .get(index + 1)
            .map(|value| i32::from(*value))
            .or_else(|| next_sample.map(i32::from))
            .unwrap_or(current);
        output[base] = *sample;
        output[base + 1] = interpolate_i16(current, next, 1, 3);
        output[base + 2] = interpolate_i16(current, next, 2, 3);
    }
    Ok(output)
}

fn interpolate_i16(start: i32, end: i32, numerator: i32, denominator: i32) -> i16 {
    let ratio = numerator as f64 / denominator as f64;
    let value = start as f64 + (end - start) as f64 * ratio;
    value
        .round()
        .clamp(f64::from(i16::MIN), f64::from(i16::MAX)) as i16
}

fn get_u16(bytes: &[u8], offset: usize) -> u16 {
    u16::from_le_bytes([bytes[offset], bytes[offset + 1]])
}

fn get_u32(bytes: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes([
        bytes[offset],
        bytes[offset + 1],
        bytes[offset + 2],
        bytes[offset + 3],
    ])
}

fn get_u64(bytes: &[u8], offset: usize) -> u64 {
    u64::from_le_bytes([
        bytes[offset],
        bytes[offset + 1],
        bytes[offset + 2],
        bytes[offset + 3],
        bytes[offset + 4],
        bytes[offset + 5],
        bytes[offset + 6],
        bytes[offset + 7],
    ])
}

fn put_u16(bytes: &mut [u8], offset: usize, value: u16) {
    bytes[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
}

fn put_u32(bytes: &mut [u8], offset: usize, value: u32) {
    bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}

fn put_u64(bytes: &mut [u8], offset: usize, value: u64) {
    bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}

pub fn ensure_internal_audio_shape(sample_rate: u32, frame_ms: u32) -> Result<()> {
    if sample_rate != 16_000 || frame_ms != 10 {
        bail!(
            "audio_frontend tcp capture currently expects RustClient internal audio to be 16kHz/10ms, got {sample_rate}Hz/{frame_ms}ms"
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn valid_header_bytes() -> [u8; HEADER_BYTES] {
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
        bytes[24..32].copy_from_slice(&42u64.to_le_bytes());
        bytes[32..40].copy_from_slice(&123_456u64.to_le_bytes());
        bytes[40..44].copy_from_slice(&(WIRE_PAYLOAD_BYTES as u32).to_le_bytes());
        bytes
    }

    #[test]
    fn validates_mic_header() {
        let header = AudioFrontendHeader::decode(&valid_header_bytes());
        header.validate(FRAME_TYPE_MIC).unwrap();
        assert_eq!(header.seq, 42);
        assert_eq!(header.timestamp_us, 123_456);
    }

    #[test]
    fn rejects_wrong_frame_type() {
        let header = AudioFrontendHeader::decode(&valid_header_bytes());
        let err = header.validate(FRAME_TYPE_SPEAKER).unwrap_err();
        assert!(err.to_string().contains("frame type"));
    }

    #[test]
    fn downsample_averages_each_three_sample_group() {
        let mut samples = [0i16; WIRE_SAMPLES_PER_CHANNEL];
        samples[0] = 3;
        samples[1] = 6;
        samples[2] = 9;
        samples[3] = -3;
        samples[4] = -6;
        samples[5] = -9;

        let output = downsample_48k_to_16k(&samples);

        assert_eq!(output.len(), 160);
        assert_eq!(output[0], 6);
        assert_eq!(output[1], -6);
    }

    #[test]
    fn encodes_speaker_header_and_payload() {
        let header = AudioFrontendHeader::speaker(7, 123_456);
        let encoded = header.encode();
        let decoded = AudioFrontendHeader::decode(&encoded);

        decoded.validate(FRAME_TYPE_SPEAKER).unwrap();
        assert_eq!(decoded.seq, 7);
        assert_eq!(decoded.timestamp_us, 123_456);

        let mut samples = [0i16; WIRE_SAMPLES_PER_CHANNEL];
        samples[0] = 123;
        samples[1] = -456;
        let payload = encode_s16le_payload(&samples);
        let decoded_payload = decode_s16le_payload(&payload);
        assert_eq!(decoded_payload[0], 123);
        assert_eq!(decoded_payload[1], -456);
    }

    #[test]
    fn repeat_upsample_repeats_each_16k_sample_three_times() {
        let mut samples = vec![0i16; WIRE_SAMPLES_PER_CHANNEL / 3];
        samples[0] = 11;
        samples[1] = -22;

        let output = repeat_upsample_16k_to_48k(&samples).unwrap();

        assert_eq!(&output[0..3], &[11, 11, 11]);
        assert_eq!(&output[3..6], &[-22, -22, -22]);
    }

    #[test]
    fn upsample_interpolates_between_16k_samples() {
        let mut samples = vec![0i16; WIRE_SAMPLES_PER_CHANNEL / 3];
        samples[0] = 0;
        samples[1] = 30;

        let output = upsample_16k_to_48k(&samples).unwrap();

        assert_eq!(&output[0..3], &[0, 10, 20]);
        assert_eq!(&output[3..6], &[30, 20, 10]);
    }

    #[test]
    fn upsample_holds_last_sample_at_frame_end() {
        let mut samples = vec![0i16; WIRE_SAMPLES_PER_CHANNEL / 3];
        samples[159] = 42;

        let output = upsample_16k_to_48k(&samples).unwrap();

        assert_eq!(&output[477..480], &[42, 42, 42]);
    }

    #[test]
    fn upsample_can_interpolate_frame_boundary_with_next_sample() {
        let mut samples = vec![0i16; WIRE_SAMPLES_PER_CHANNEL / 3];
        samples[159] = 3000;

        let output = upsample_16k_to_48k_with_next(&samples, Some(6000)).unwrap();

        assert_eq!(output[477], 3000);
        assert_eq!(output[478], 4000);
        assert_eq!(output[479], 5000);
    }
}
