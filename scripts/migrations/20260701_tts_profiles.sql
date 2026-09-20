-- Introduce Bot-bound TTS Profiles for v3.0.
-- This migration intentionally leaves legacy bot.tts_voice / bot.tts_speech_rate
-- columns in place for rollback safety; runtime code no longer reads them.

create table if not exists tts_profiles (
  id bigint primary key auto_increment,
  tts_id varchar(100) not null unique,
  tts_name varchar(100) not null,
  provider_type varchar(50) not null,
  speed double not null default 1.0,
  provider_config_json longtext not null,
  enabled boolean not null default true,
  updated_at timestamp not null default current_timestamp on update current_timestamp
) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci;

insert into tts_profiles (
  tts_id, tts_name, provider_type, speed, provider_config_json, enabled
)
values (
  'default_tts_profile',
  'Default CustomVoice',
  'qwen3_custom_voice',
  1.0,
  '{"voice":"serena","instruct":"自然、温柔、稳定、口语化，语速适中，情绪轻微，不夸张。"}',
  true
)
on duplicate key update tts_id = tts_id;

alter table bots
  add column tts_profile_id varchar(100) not null default 'default_tts_profile';

update bots
set tts_profile_id = 'default_tts_profile'
where trim(coalesce(tts_profile_id, '')) = '';
