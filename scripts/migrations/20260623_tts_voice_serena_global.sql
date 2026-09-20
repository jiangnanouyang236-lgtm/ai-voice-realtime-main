-- Global TTS voice migration for local Qwen3-TTS.
-- Safe to run more than once. It makes Serena the DB default and normalizes
-- existing BotTemplate rows so all robots use the same voice by default.

alter table bots alter column tts_voice set default 'serena';

update bots
set tts_voice = 'serena',
    updated_at = now()
where lower(trim(tts_voice)) <> 'serena'
   or trim(tts_voice) = '';
