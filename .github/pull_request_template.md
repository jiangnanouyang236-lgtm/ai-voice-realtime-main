## Summary

<!-- What changed and why? -->

## Scope

- [ ] Client
- [ ] Gateway
- [ ] LLM
- [ ] STT
- [ ] TTS
- [ ] MCP
- [ ] Admin UI
- [ ] Docs / Config

## Config Changes

- [ ] No environment/config changes
- [ ] `.env.example` updated
- [ ] Admin UI / DB config migration needed

Notes:

## Verification

- [ ] `python scripts/ai_preflight.py --profile offline`
- [ ] `python scripts/check_repo_hygiene.py --index`（检查已暂存内容）
- [ ] `python -m compileall gateway llm mcp_servers server_config stt tts scripts -q`
- [ ] `cargo check` in `rust_client`
- [ ] `npm run build` in `admin-ui/frontend`
- [ ] Manual voice round tested
- [ ] MCP / Agent behavior tested
- [ ] 若修改 `data/eval_gold/`，已运行 Gold provenance gate；正式 acceptance 已由 owner 审核并更新 manifest

Commands and results:

Runtime profile (`offline` / `local` / `compose-v3` / `lan` / `hardware` / `model-host`):

Evidence level (`IMPLEMENTED` / `LOCAL_VERIFIED` / `HYBRID_VERIFIED` / `LIVE_VERIFIED` / `HARDWARE_VERIFIED`):

Evidence report path and validation command (required for `LOCAL_VERIFIED` or higher):

```text
python scripts/validate_evidence_report.py <report.json>
```

## Rollback Notes

<!-- How do we roll this back if needed? -->
