SHELL := /bin/bash

DEPLOY_HOST ?= oracle-1
REMOTE_DIR ?= mini-agent-lab
PORT ?= 8080

RSYNC_EXCLUDES := \
	--exclude '.git' \
	--exclude '.venv' \
	--exclude '__pycache__' \
	--exclude '*.pyc' \
	--exclude '.pytest_cache' \
	--exclude '.ruff_cache' \
	--exclude 'eval_reports' \
	--exclude 'logs' \
	--exclude '.env'

.PHONY: test run-local deploy smoke logs down config

## 本地离线测试（零 API 消耗）
test:
	uv run pytest -q

## 本地 mock 模式起服务：http://127.0.0.1:$(PORT)
run-local:
	MOCK_LLM=1 PORT=$(PORT) LOG_DIR=logs uv run python -m s05_service.main

## 校验 compose 配置（本机需有 docker compose）
config:
	docker compose -f deploy/docker-compose.yml config -q

## 同步代码到远程主机 -> 远程构建镜像 -> 启动容器 -> 自动 smoke
deploy:
	rsync -az --delete $(RSYNC_EXCLUDES) ./ $(DEPLOY_HOST):$(REMOTE_DIR)/
	@if [ -f deploy/.env ]; then rsync -az deploy/.env $(DEPLOY_HOST):$(REMOTE_DIR)/deploy/.env; fi
	ssh $(DEPLOY_HOST) 'cd $(REMOTE_DIR)/deploy && docker compose build && AGENT_PORT=$(PORT) docker compose up -d --remove-orphans'
	$(MAKE) smoke

## 通过 ssh 在远程主机回环地址执行 curl 冒烟（不暴露公网端口）
smoke:
	bash deploy/smoke.sh $(DEPLOY_HOST) $(PORT)

## 查看 JSONL 请求日志（命名卷）
logs:
	ssh $(DEPLOY_HOST) 'cd $(REMOTE_DIR)/deploy && docker compose exec -T agent-lab tail -n 20 /app/logs/requests.jsonl'

## 停止并移除容器
down:
	ssh $(DEPLOY_HOST) 'cd $(REMOTE_DIR)/deploy && docker compose down'