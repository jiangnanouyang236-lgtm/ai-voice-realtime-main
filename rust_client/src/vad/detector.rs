use anyhow::Result;
use webrtc_vad::{SampleRate, Vad, VadMode};

pub struct VadDetector {
    vad: Vad,
}

impl VadDetector {
    pub fn new(mode: u8, sample_rate: u32) -> Result<Self> {
        let mut vad = Vad::new();
        vad.set_mode(match mode {
            0 => VadMode::Quality,
            1 => VadMode::LowBitrate,
            2 => VadMode::Aggressive,
            _ => VadMode::VeryAggressive,
        });
        vad.set_sample_rate(match sample_rate {
            8000 => SampleRate::Rate8kHz,
            16000 => SampleRate::Rate16kHz,
            32000 => SampleRate::Rate32kHz,
            48000 => SampleRate::Rate48kHz,
            _ => SampleRate::Rate16kHz,
        });
        Ok(Self { vad })
    }

    pub fn is_speech(&mut self, frame: &[i16]) -> Result<bool> {
        self.vad
            .is_voice_segment(frame)
            .map_err(|_| anyhow::anyhow!("VAD 检测失败"))
    }

    pub fn dbfs(&self, frame: &[i16]) -> f32 {
        if frame.is_empty() {
            return -90.0;
        }
        let sum_sq = frame
            .iter()
            .map(|sample| {
                let v = *sample as f32;
                v * v
            })
            .sum::<f32>();
        let rms = (sum_sq / frame.len() as f32).sqrt();
        if rms <= 1e-6 {
            return -90.0;
        }
        20.0 * (rms / 32768.0).log10()
    }

    pub fn is_start_trigger_speech(&mut self, frame: &[i16], min_dbfs: f32) -> Result<bool> {
        if !self.is_speech(frame)? {
            return Ok(false);
        }
        Ok(self.dbfs(frame) >= min_dbfs)
    }
}
