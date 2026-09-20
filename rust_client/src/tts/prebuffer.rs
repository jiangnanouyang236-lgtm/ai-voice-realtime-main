pub struct TtsPrebuffer {
    target_samples: usize,
    buffered_samples: usize,
    chunks: Vec<Vec<i16>>,
    playback_started: bool,
}

impl TtsPrebuffer {
    pub fn new(target_samples: usize) -> Self {
        Self {
            target_samples,
            buffered_samples: 0,
            chunks: Vec::new(),
            playback_started: false,
        }
    }

    pub fn push(&mut self, chunk: Vec<i16>) -> Option<Vec<Vec<i16>>> {
        if self.playback_started || self.target_samples == 0 {
            self.playback_started = true;
            return Some(vec![chunk]);
        }

        self.buffered_samples += chunk.len();
        self.chunks.push(chunk);
        if self.buffered_samples >= self.target_samples {
            self.playback_started = true;
            return Some(self.flush_all());
        }
        None
    }

    pub fn flush_all(&mut self) -> Vec<Vec<i16>> {
        self.buffered_samples = 0;
        std::mem::take(&mut self.chunks)
    }

    pub fn clear(&mut self) {
        self.buffered_samples = 0;
        self.chunks.clear();
        self.playback_started = false;
    }

    pub fn is_playback_started(&self) -> bool {
        self.playback_started
    }
}
