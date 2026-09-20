use crate::audio::resample::resample_linear_i16;
use anyhow::{bail, Context, Result};
use std::path::Path;

pub fn load_wav_for_playback(path: &Path, playback_rate: u32) -> Result<Vec<i16>> {
    let mut reader = hound::WavReader::open(path)
        .with_context(|| format!("打开 WAV 文件失败: {}", path.display()))?;
    let spec = reader.spec();
    if spec.sample_format != hound::SampleFormat::Int || spec.bits_per_sample != 16 {
        bail!(
            "仅支持 16-bit PCM WAV: {} ({:?}, {} bits)",
            path.display(),
            spec.sample_format,
            spec.bits_per_sample
        );
    }

    let channels = spec.channels.max(1) as usize;
    let samples = reader
        .samples::<i16>()
        .collect::<std::result::Result<Vec<_>, _>>()
        .with_context(|| format!("读取 WAV 样本失败: {}", path.display()))?;
    let mono_samples = if channels == 1 {
        samples
    } else {
        samples
            .chunks(channels)
            .map(|frame| {
                let sum: i32 = frame.iter().map(|sample| *sample as i32).sum();
                (sum / frame.len() as i32).clamp(i16::MIN as i32, i16::MAX as i32) as i16
            })
            .collect()
    };

    Ok(resample_linear_i16(
        &mono_samples,
        spec.sample_rate,
        playback_rate,
    ))
}
