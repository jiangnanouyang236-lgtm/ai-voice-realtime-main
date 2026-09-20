use anyhow::{Context, Result};
use rubato::{
    Resampler, SincFixedIn, SincInterpolationParameters, SincInterpolationType, WindowFunction,
};

/// 线性重采样（对齐 Python np.interp，用于兼容旧 TTS 采样率）
pub fn resample_linear_i16(input: &[i16], from_rate: u32, to_rate: u32) -> Vec<i16> {
    if from_rate == to_rate || input.is_empty() {
        return input.to_vec();
    }
    let new_len = (input.len() as u64 * to_rate as u64 / from_rate as u64) as usize;
    if new_len == 0 {
        return Vec::new();
    }
    let mut output = Vec::with_capacity(new_len);
    for i in 0..new_len {
        let src_pos = i as f64 * input.len() as f64 / new_len as f64;
        let lo = src_pos.floor() as usize;
        let hi = (lo + 1).min(input.len() - 1);
        let frac = src_pos - lo as f64;
        let sample = input[lo] as f64 * (1.0 - frac) + input[hi] as f64 * frac;
        output.push(sample.round().clamp(-32768.0, 32767.0) as i16);
    }
    output
}

/// 高质量输入重采样器（用于麦克风采集侧，设备采样率 → 16kHz）
pub struct InputResampler {
    resampler: SincFixedIn<f32>,
    input_channels: usize,
    input_chunk_frames: usize,
    output_buffer: Vec<f32>,
}

impl InputResampler {
    pub fn new(
        input_sample_rate: u32,
        output_sample_rate: u32,
        input_channels: usize,
    ) -> Result<Self> {
        let params = SincInterpolationParameters {
            sinc_len: 256,
            f_cutoff: 0.95,
            interpolation: SincInterpolationType::Linear,
            oversampling_factor: 256,
            window: WindowFunction::BlackmanHarris2,
        };

        let input_chunk_frames = 1024;
        let ratio = output_sample_rate as f64 / input_sample_rate as f64;
        let resampler =
            SincFixedIn::<f32>::new(ratio, 2.0, params, input_chunk_frames, input_channels)
                .context("创建输入重采样器失败")?;

        Ok(Self {
            resampler,
            input_channels,
            input_chunk_frames,
            output_buffer: Vec::new(),
        })
    }

    pub fn input_chunk_frames(&self) -> usize {
        self.input_chunk_frames
    }

    pub fn process_interleaved(&mut self, interleaved: &[f32]) -> Result<Vec<f32>> {
        if interleaved.is_empty() {
            return Ok(Vec::new());
        }

        let mut planar = vec![Vec::new(); self.input_channels];
        for frame in interleaved.chunks(self.input_channels) {
            for (ch, sample) in frame.iter().enumerate() {
                planar[ch].push(*sample);
            }
        }

        let resampled = self
            .resampler
            .process(&planar, None)
            .context("输入重采样失败")?;
        let out_frames = resampled.first().map(|c| c.len()).unwrap_or(0);
        let mut mixed = Vec::with_capacity(out_frames);

        for frame_idx in 0..out_frames {
            let mut acc = 0.0f32;
            for channel in &resampled {
                acc += channel[frame_idx];
            }
            mixed.push(acc / self.input_channels as f32);
        }

        Ok(mixed)
    }

    pub fn push_output(&mut self, samples: &[f32]) {
        self.output_buffer.extend_from_slice(samples);
    }

    pub fn drain_frames(&mut self, frame_size: usize) -> Vec<Vec<i16>> {
        let mut frames = Vec::new();
        while self.output_buffer.len() >= frame_size {
            let chunk: Vec<f32> = self.output_buffer.drain(..frame_size).collect();
            let frame = chunk
                .into_iter()
                .map(|s| (s * 32767.0).clamp(-32768.0, 32767.0) as i16)
                .collect();
            frames.push(frame);
        }
        frames
    }
}
