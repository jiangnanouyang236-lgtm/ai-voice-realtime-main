-- Local Qwen3-TTS voice migration for existing config databases.
-- Safe to run more than once. It changes legacy Qwen Realtime voices to a
-- Serena, the current global voice baseline.

alter table bots alter column tts_voice set default 'serena';

update bots
set tts_voice = 'serena',
    updated_at = now()
where lower(tts_voice) in ('cherry', 'stella', 'luna')
   or trim(tts_voice) = '';
