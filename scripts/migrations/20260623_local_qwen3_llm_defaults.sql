-- Local Qwen3.5-9B LLM migration for existing config databases.
-- Safe to run more than once. It moves legacy DashScope model names to the
-- local vLLM model that the voice stack now uses by default.

alter table bots alter column model set default 'qwen3-5-9b';

update bots
set model = 'qwen3-5-9b',
    updated_at = now()
where lower(model) in ('qwen-plus', 'qwen-max')
   or trim(model) = '';
