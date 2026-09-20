use anyhow::Result;
use std::collections::VecDeque;

pub struct VadTriggerWindow {
    frames: VecDeque<Vec<i16>>,
    flags: VecDeque<bool>,
    trigger_ratio: f32,
    capacity: usize,
}

impl VadTriggerWindow {
    pub fn new(capacity: usize, trigger_ratio: f32) -> Result<Self> {
        anyhow::ensure!(capacity > 0, "VAD trigger window 容量必须大于 0");
        Ok(Self {
            frames: VecDeque::with_capacity(capacity),
            flags: VecDeque::with_capacity(capacity),
            trigger_ratio,
            capacity,
        })
    }

    pub fn push(&mut self, frame: Vec<i16>, is_trigger_speech: bool) {
        if self.frames.len() == self.capacity {
            self.frames.pop_front();
            self.flags.pop_front();
        }
        self.frames.push_back(frame);
        self.flags.push_back(is_trigger_speech);
    }

    pub fn should_trigger(&self) -> bool {
        if self.frames.len() < self.capacity {
            return false;
        }
        let speech_count = self.flags.iter().filter(|&&flag| flag).count() as f32;
        speech_count >= self.trigger_ratio * self.capacity as f32
    }

    pub fn drain_frames(&mut self) -> Vec<Vec<i16>> {
        self.flags.clear();
        self.frames.drain(..).collect()
    }

    pub fn clear(&mut self) {
        self.frames.clear();
        self.flags.clear();
    }
}
