create table if not exists bots (
  id bigint primary key auto_increment,
  bot_id varchar(100) not null unique,
  name varchar(100) not null,
  system_prompt text not null,
  model varchar(100) not null,
  temperature double not null default 0.7,
  max_tokens int not null default 2000,
  max_response_chars int not null default 0,
  tts_profile_id varchar(100) not null default 'default_tts_profile',
  enabled boolean not null default true,
  is_default boolean not null default false,
  updated_at timestamp not null default current_timestamp on update current_timestamp
) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci;

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

create table if not exists robots (
  id bigint primary key auto_increment,
  robot_id varchar(100) not null unique,
  name varchar(100) not null,
  assigned_bot_id varchar(100) not null,
  client_type varchar(50),
  enabled boolean not null default true,
  is_new boolean not null default true,
  robot_secret_hash text,
  robot_secret_updated_at timestamp null,
  last_connected_at timestamp null,
  notes text,
  updated_at timestamp not null default current_timestamp on update current_timestamp,
  key idx_robots_assigned_bot_id (assigned_bot_id),
  constraint fk_robots_assigned_bot_id foreign key (assigned_bot_id) references bots(bot_id)
) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci;

create table if not exists mcp_servers (
  id bigint primary key auto_increment,
  server_key varchar(100) not null unique,
  display_name varchar(100) not null,
  type varchar(50) not null,
  url text,
  command text,
  args_json longtext not null,
  headers_json longtext not null,
  enabled boolean not null default true,
  updated_at timestamp not null default current_timestamp on update current_timestamp
) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci;

create table if not exists bot_mcp_bindings (
  id bigint primary key auto_increment,
  bot_id bigint not null,
  mcp_server_id bigint not null,
  sort_order int not null default 0,
  unique key uk_bot_mcp_binding (bot_id, mcp_server_id),
  key idx_bot_mcp_bindings_bot_id (bot_id),
  constraint fk_bot_mcp_bindings_bot_id foreign key (bot_id) references bots(id) on delete cascade,
  constraint fk_bot_mcp_bindings_mcp_server_id foreign key (mcp_server_id) references mcp_servers(id) on delete cascade
) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci;

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

create table if not exists config_snapshots (
  id bigint primary key auto_increment,
  config_version bigint not null unique,
  snapshot_json longtext not null,
  created_at timestamp not null default current_timestamp,
  created_by varchar(100),
  key idx_config_snapshots_version_desc (config_version)
) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci;

create table if not exists service_configs (
  service_name varchar(50) primary key,
  config_json longtext not null,
  updated_at timestamp not null default current_timestamp on update current_timestamp
) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci;
