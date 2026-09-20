use std::time::Instant;

#[derive(Debug, Clone)]
pub enum DialogueState {
    WaitingForWakeWord,
    AwakeIdle {
        last_interaction: Instant,
    },
    Recording {
        started_at: Instant,
        last_interaction: Instant,
    },
    SendingAudio {
        last_interaction: Instant,
        waiting_started_at: Instant,
    },
    TtsPlaying,
}

impl DialogueState {
    pub fn can_start_tts_playback(&self) -> bool {
        matches!(self, DialogueState::SendingAudio { .. })
    }

    pub fn accepts_server_round_messages(&self) -> bool {
        matches!(
            self,
            DialogueState::SendingAudio { .. } | DialogueState::TtsPlaying
        )
    }
}
