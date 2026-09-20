-- Agent Runtime v2 migration for existing MySQL/MariaDB config databases.
-- Safe to run more than once. It only creates the Agent catalog and Bot-Agent binding tables.

create table if not exists agents (
  id bigint primary key auto_increment,
  agent_id varchar(100) not null unique,
  name varchar(100) not null,
  description text,
  module varchar(200),
  class_name varchar(100),
  enabled boolean not null default true,
  enabled_by_default boolean not null default true,
  trigger_examples_json longtext not null,
  allowed_tools_json longtext not null,
  tool_groups_json longtext not null,
  updated_at timestamp not null default current_timestamp on update current_timestamp
) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci;

create table if not exists bot_agent_bindings (
  id bigint primary key auto_increment,
  bot_id bigint not null,
  agent_id bigint not null,
  sort_order int not null default 0,
  unique key uk_bot_agent_binding (bot_id, agent_id),
  key idx_bot_agent_bindings_bot_id (bot_id),
  constraint fk_bot_agent_bindings_bot_id foreign key (bot_id) references bots(id) on delete cascade,
  constraint fk_bot_agent_bindings_agent_id foreign key (agent_id) references agents(id) on delete cascade
) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci;
