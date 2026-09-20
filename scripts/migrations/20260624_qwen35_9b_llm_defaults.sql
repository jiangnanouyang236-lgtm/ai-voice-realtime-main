-- Switch the local LLM baseline from Qwen3-14B to Qwen3.5-9B.
-- Safe to run more than once.

alter table bots alter column model set default 'qwen3-5-9b';

update bots
set model = 'qwen3-5-9b',
    updated_at = now()
where lower(trim(model)) in ('qwen3-14b', 'qwen-plus', 'qwen-max')
   or trim(model) = '';
