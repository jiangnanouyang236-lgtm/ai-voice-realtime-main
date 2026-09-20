-- Add an application-side spoken response character budget per Bot.
-- Safe to run more than once on MySQL / MariaDB config databases.
-- A value of 0 means unlimited; positive values cap user-visible LLM text per turn.

set @bot_max_response_chars_column_exists := (
  select count(*)
  from information_schema.columns
  where table_schema = database()
    and table_name = 'bots'
    and column_name = 'max_response_chars'
);

set @bot_max_response_chars_sql := if(
  @bot_max_response_chars_column_exists = 0,
  'alter table bots add column max_response_chars int not null default 0 after max_tokens',
  'select 1'
);

prepare bot_max_response_chars_stmt from @bot_max_response_chars_sql;
execute bot_max_response_chars_stmt;
deallocate prepare bot_max_response_chars_stmt;
