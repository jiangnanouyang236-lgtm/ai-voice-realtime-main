-- Remove legacy environment placeholders from persisted server-config rows.
-- Safe to run more than once. Runtime config now expects the current variable
-- names directly instead of resolving QWEN_LLM_* aliases.

update mcp_servers
set
  url = case
    when url is null then null
    else replace(url, '${QWEN_LLM_BASE_URL}', '${LLM_BASE_URL}')
  end,
  command = case
    when command is null then null
    else replace(command, '${QWEN_LLM_BASE_URL}', '${LLM_BASE_URL}')
  end,
  args_json = replace(
    replace(args_json, '${QWEN_LLM_BASE_URL}', '${LLM_BASE_URL}'),
    '${QWEN_LLM_MODEL_NAME}',
    '${LLM_MODEL_NAME}'
  ),
  headers_json = replace(headers_json, '${QWEN_LLM_API_KEY}', '${WEBSEARCH_API_KEY}'),
  updated_at = now()
where coalesce(url, '') like '%${QWEN_LLM_BASE_URL}%'
   or coalesce(command, '') like '%${QWEN_LLM_BASE_URL}%'
   or args_json like '%${QWEN_LLM_BASE_URL}%'
   or args_json like '%${QWEN_LLM_MODEL_NAME}%'
   or headers_json like '%${QWEN_LLM_API_KEY}%';
