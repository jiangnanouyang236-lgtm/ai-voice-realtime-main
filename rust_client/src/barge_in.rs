#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BargeInIdentity {
    pub utterance_id: String,
    pub round_id: String,
    pub playback_id: String,
    pub speech_epoch: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BargeInProbe {
    pub candidate_seq: u64,
    pub speech_epoch: u64,
    pub audio_watermark: u64,
}

#[derive(Debug, Clone)]
pub struct BargeInProbeSchedule {
    identity: BargeInIdentity,
    first_probe_samples: u64,
    update_probe_samples: u64,
    candidate_seq: u64,
}

impl BargeInProbeSchedule {
    pub fn new(identity: BargeInIdentity, sample_rate: u32) -> Self {
        Self {
            identity,
            first_probe_samples: u64::from(sample_rate) * 800 / 1_000,
            update_probe_samples: u64::from(sample_rate) * 1_200 / 1_000,
            candidate_seq: 0,
        }
    }

    pub fn identity(&self) -> &BargeInIdentity {
        &self.identity
    }

    pub fn next_probe(&mut self, total_samples: u64) -> Option<BargeInProbe> {
        let due = match self.candidate_seq {
            0 => self.first_probe_samples,
            1 => self.update_probe_samples,
            _ => return None,
        };
        if total_samples < due {
            return None;
        }
        self.candidate_seq += 1;
        Some(BargeInProbe {
            candidate_seq: self.candidate_seq,
            speech_epoch: self.identity.speech_epoch,
            audio_watermark: total_samples,
        })
    }

    pub fn current_candidate_seq(&self) -> u64 {
        self.candidate_seq
    }

    pub fn matches(
        &self,
        utterance_id: &str,
        round_id: &str,
        playback_id: &str,
        candidate_seq: u64,
        speech_epoch: u64,
        audio_watermark: u64,
        uploaded_samples: u64,
    ) -> bool {
        self.identity.utterance_id == utterance_id
            && self.identity.round_id == round_id
            && self.identity.playback_id == playback_id
            && self.identity.speech_epoch == speech_epoch
            && self.candidate_seq == candidate_seq
            && audio_watermark > 0
            && audio_watermark <= uploaded_samples
    }
}

#[cfg(test)]
mod tests {
    use super::{BargeInIdentity, BargeInProbeSchedule};

    fn schedule() -> BargeInProbeSchedule {
        BargeInProbeSchedule::new(
            BargeInIdentity {
                utterance_id: "utt-1".to_string(),
                round_id: "round-1".to_string(),
                playback_id: "playback-1".to_string(),
                speech_epoch: 1,
            },
            16_000,
        )
    }

    #[test]
    fn probes_once_at_800ms_and_once_at_1200ms() {
        let mut schedule = schedule();
        assert!(schedule.next_probe(12_799).is_none());
        let first = schedule.next_probe(12_800).unwrap();
        assert_eq!(first.candidate_seq, 1);
        assert_eq!(first.audio_watermark, 12_800);
        assert!(schedule.next_probe(19_199).is_none());
        let update = schedule.next_probe(19_200).unwrap();
        assert_eq!(update.candidate_seq, 2);
        assert!(schedule.next_probe(32_000).is_none());
    }

    #[test]
    fn exact_identity_and_uploaded_watermark_are_required() {
        let mut schedule = schedule();
        let probe = schedule.next_probe(12_800).unwrap();
        assert!(schedule.matches(
            "utt-1",
            "round-1",
            "playback-1",
            probe.candidate_seq,
            probe.speech_epoch,
            probe.audio_watermark,
            12_800,
        ));
        assert!(!schedule.matches(
            "utt-1",
            "round-1",
            "old-playback",
            probe.candidate_seq,
            probe.speech_epoch,
            probe.audio_watermark,
            12_800,
        ));
        assert!(!schedule.matches(
            "utt-1",
            "round-1",
            "playback-1",
            probe.candidate_seq,
            probe.speech_epoch,
            probe.audio_watermark,
            12_799,
        ));
    }
}
