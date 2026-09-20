# Nohup 日志管理方案

这个方案用于“暂时还没有 systemd / docker-compose”的部署阶段：继续用后台进程启动服务，但把日志拆分到不同目录，并用轻量脚本做日志滚动，避免 `nohup.out` 无限增长。

## 目录结构

默认会生成：

```text
logs/
  gateway/gateway.log
  llm/llm.log
  stt/stt.log
  tts/tts.log
  admin/admin.log
  mcp-robot/mcp-robot.log
  mcp-singing/mcp-singing.log
  mcp-utils/mcp-utils.log
  log-rotator.log
run/
  gateway.pid
  llm.pid
  ...
```

`logs/` 和 `run/` 已加入 `.gitignore`。

## 常用命令

查看可管理的服务：

```bash
scripts/nohup_service.sh list
```

启动默认服务组：

```bash
scripts/nohup_service.sh start all
```

默认服务组包含：

```text
stt llm tts gateway admin mcp-robot mcp-singing mcp-utils
```

单独启动某个服务：

```bash
scripts/nohup_service.sh start gateway
scripts/nohup_service.sh start mcp-robot
```

查看状态：

```bash
scripts/nohup_service.sh status all
scripts/nohup_service.sh status gateway
```

查看日志：

```bash
scripts/nohup_service.sh tail gateway
scripts/nohup_service.sh tail llm 500
```

停止或重启：

```bash
scripts/nohup_service.sh stop gateway
scripts/nohup_service.sh restart gateway
```

## 日志滚动

脚本启动服务时会自动启动一个轻量 rotator。默认每 60 秒检查一次日志，单个日志超过 100MB 时滚动，保留 5 个历史文件。

滚动方式是 `copytruncate`：先复制当前日志到 `.1`，再清空原日志文件。这样即使服务进程仍然持有原文件句柄，也会继续写入被清空后的当前日志文件。

手动滚动：

```bash
scripts/nohup_service.sh rotate all
scripts/nohup_service.sh rotate gateway
```

单独启动/停止 rotator：

```bash
scripts/nohup_service.sh start-rotator
scripts/nohup_service.sh stop-rotator
```

## 可调参数

```bash
# 日志目录，默认 ./logs
export LOG_DIR=/data/windaka/logs

# pid 目录，默认 ./run
export RUN_DIR=/data/windaka/run

# 单个日志超过多少 MB 触发滚动，默认 100
export LOG_MAX_SIZE_MB=100

# 每个服务保留几个历史日志，默认 5
export LOG_BACKUPS=5

# rotator 检查间隔，默认 60 秒
export LOG_ROTATE_INTERVAL_SEC=60

# 启动服务前是否 source 项目根目录 .env，默认 1
export LOAD_ENV=1
```

示例：

```bash
LOG_DIR=/data/windaka/logs LOG_MAX_SIZE_MB=50 scripts/nohup_service.sh start all
```

## 注意

- 这个脚本主要解决日志管理，不负责服务异常后的自动拉起。
- 后续迁移到 docker-compose 后，建议使用 Docker logging driver 的 `max-size` / `max-file` 做日志轮转。
- 如果你已经手动 `nohup` 启动了旧进程，建议先手动停止旧进程，再用这个脚本统一启动，避免端口占用和日志分散。
