/// 与 Python amplify_audio 对齐的 AGC + 固定增益处理
pub fn apply_agc_and_gain(
    samples: &[i16],
    target_db: f32,
    compression_ratio: f32,
    fixed_gain: f32,
) -> Vec<i16> {
    if samples.is_empty() {
        return Vec::new();
    }

    let mut audio: Vec<f32> = samples.iter().map(|&s| s as f32).collect();

    // AGC：RMS → dBFS → 线性增益
    let sum_sq: f32 = audio.iter().map(|s| s * s).sum();
    let rms = (sum_sq / audio.len() as f32).sqrt();
    if rms > 0.0 {
        let current_db = 20.0 * (rms / 32768.0).log10();
        let gain_db = target_db - current_db;
        let linear_gain = (10.0_f32.powf(gain_db / 20.0)).clamp(0.1, 10.0);
        for s in &mut audio {
            *s *= linear_gain;
        }

        // 动态压缩
        if compression_ratio > 1.0 {
            let threshold_linear = 32768.0 * 10.0_f32.powf(target_db / 20.0);
            for s in &mut audio {
                let abs_s = s.abs();
                if abs_s > threshold_linear {
                    let excess = abs_s - threshold_linear;
                    let compressed = threshold_linear + excess / compression_ratio;
                    *s = s.signum() * compressed;
                }
            }
        }
    }

    // 固定增益
    if fixed_gain != 1.0 {
        for s in &mut audio {
            *s *= fixed_gain;
        }
    }

    audio
        .into_iter()
        .map(|s| s.clamp(-32768.0, 32767.0) as i16)
        .collect()
}
