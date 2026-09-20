-- Remove local stdio MCP servers and keep Bot bindings on remote MCP only.
-- Safe to run more than once.

insert ignore into bot_mcp_bindings (bot_id, mcp_server_id, sort_order)
select bm.bot_id, remote_ms.id, bm.sort_order
from bot_mcp_bindings bm
join mcp_servers local_ms on local_ms.id = bm.mcp_server_id
join mcp_servers remote_ms on remote_ms.server_key = 'utils_remote'
where local_ms.server_key = 'utils_local';

insert ignore into bot_mcp_bindings (bot_id, mcp_server_id, sort_order)
select bm.bot_id, remote_ms.id, bm.sort_order
from bot_mcp_bindings bm
join mcp_servers local_ms on local_ms.id = bm.mcp_server_id
join mcp_servers remote_ms on remote_ms.server_key = 'robot_remote'
where local_ms.server_key = 'robot_local';

insert ignore into bot_mcp_bindings (bot_id, mcp_server_id, sort_order)
select bm.bot_id, remote_ms.id, bm.sort_order
from bot_mcp_bindings bm
join mcp_servers local_ms on local_ms.id = bm.mcp_server_id
join mcp_servers remote_ms on remote_ms.server_key = 'websearch'
where local_ms.server_key = 'weather_local';

insert ignore into bot_mcp_bindings (bot_id, mcp_server_id, sort_order)
select b.id, ms.id, 0
from bots b
join mcp_servers ms on ms.server_key = 'utils_remote'
where b.bot_id in ('default', 'xiaowen-human');

insert ignore into bot_mcp_bindings (bot_id, mcp_server_id, sort_order)
select b.id, ms.id, 1
from bots b
join mcp_servers ms on ms.server_key = 'robot_remote'
where b.bot_id in ('default', 'xiaowen-human');

insert ignore into bot_mcp_bindings (bot_id, mcp_server_id, sort_order)
select b.id, ms.id, 2
from bots b
join mcp_servers ms on ms.server_key = 'websearch'
where b.bot_id in ('default', 'xiaowen-human');

insert ignore into bot_mcp_bindings (bot_id, mcp_server_id, sort_order)
select b.id, ms.id, 3
from bots b
join mcp_servers ms on ms.server_key = 'robots_task_service'
where b.bot_id in ('default', 'xiaowen-human');

update bot_mcp_bindings bm
join bots b on b.id = bm.bot_id
join mcp_servers ms on ms.id = bm.mcp_server_id
set bm.sort_order = case ms.server_key
  when 'utils_remote' then 0
  when 'robot_remote' then 1
  when 'websearch' then 2
  when 'robots_task_service' then 3
  else bm.sort_order
end
where b.bot_id in ('default', 'xiaowen-human')
  and ms.server_key in ('utils_remote', 'robot_remote', 'websearch', 'robots_task_service');

delete bm
from bot_mcp_bindings bm
join mcp_servers ms on ms.id = bm.mcp_server_id
where ms.type = 'stdio'
   or ms.server_key in ('weather_local', 'utils_local', 'robot_local');

delete from mcp_servers
where type = 'stdio'
   or server_key in ('weather_local', 'utils_local', 'robot_local');
