# capture-pipeline — локальная эмуляция и деплой на Jetson через Docker Compose.
# Основной деплой: образ из registry (capture-up). rsync/build — только запасной путь.
.DEFAULT_GOAL := help

.PHONY: help deps test run-emulate preview-emulate preview-emulate-sudo preview-jetson \
	probe-cams jetson-auth jetson-probe jetson-nav-hz \
	capture-rsync capture-git-sync capture-build \
	capture-pull capture-up capture-down capture-logs capture-status

# --- переменные --------------------------------------------------------------

PYTHON ?= /usr/bin/python3
PIP_BREAK_SYSTEM_PACKAGES := $(shell $(PYTHON) -m pip install --help 2>/dev/null | grep -q -- --break-system-packages && echo --break-system-packages)

EMULATE_CONFIG ?= configs/laptop-emulate.yaml
# Подсказки для DOCKER_CONFIG (стенд / роверы). Пустой → resolve на TTY / .env платы.
DUO_CONFIG ?= configs/jetson-duo-one-s.yaml
ROVER_CONFIG ?= configs/jetson-delta.yaml
DOCKER_CONFIG ?=
# YAML для jetson-nav-hz (nav sink на плате).
CONFIG ?= configs/jetson-duo-one-s.yaml

# SSH на плату. Пусто → resolve_jetson_ssh.sh спросит Host из ~/.ssh/config.
JETSON_SSH ?=
JETSON_DIR ?= ~/capture-pipeline
# Пусто → jetson_docker.sh читает L4T (R36→6.2, R39→7.2).
JETPACK ?=
IMAGE_TAG ?= latest
PULL_POLICY ?= always
IMAGE ?=
# cpuset контейнера. Пусто = все ядра. Пример: 7 или 4-7
CPUSET_CPUS ?=
# SYNC_GIT=1 → перед capture-up/pull ещё git pull на плате (compose/scripts/YAML).
SYNC_GIT ?=
GIT_BRANCH ?= develop
GIT_REPO ?= git@gitlab.cyberchef.tech:last-mile-robotics/implementation/drivers/cameras/capture-pipeline.git

# Диагностика RTSP: jetson-probe (на плате) и probe-cams (с ноутбука).
PORTS ?= 8586
RTSP_IP ?= 127.0.0.1
PREVIEW_PORTS ?= 8586
PREVIEW_LABELS ?= cam0
NAV_HZ_SECS ?= 8

# PID/лог для stop native перед capture-up (jetson_remote_docker.sh).
JETSON_PID_FILE ?= /tmp/capture_pipeline.pid
JETSON_LOG ?= /tmp/capture_pipeline.log

SSH_CONTROL_PATH ?= $(HOME)/.ssh/zed-xonegs-%C
SSH_OPTIONS := -o StrictHostKeyChecking=accept-new -o IPQoS=none -o ControlMaster=auto \
	-o ControlPersist=10m -o ControlPath=$(SSH_CONTROL_PATH)
SSH = SSH_OPTIONS="$(SSH_OPTIONS)" JETSON_SSH="$(JETSON_SSH)" bash scripts/jetson_ssh.sh
REMOTE_DOCKER = SSH_OPTIONS="$(SSH_OPTIONS)" JETSON_SSH="$(JETSON_SSH)" \
	JETSON_DIR="$(JETSON_DIR)" JETSON_PID_FILE="$(JETSON_PID_FILE)" JETSON_LOG="$(JETSON_LOG)" \
	GIT_BRANCH="$(GIT_BRANCH)" GIT_REPO="$(GIT_REPO)" \
	IMAGE_TAG="$(IMAGE_TAG)" IMAGE="$(IMAGE)" JETPACK="$(JETPACK)" PULL_POLICY="$(PULL_POLICY)" \
	CPUSET_CPUS="$(CPUSET_CPUS)" DOCKER_CONFIG="$(DOCKER_CONFIG)" SYNC_GIT="$(SYNC_GIT)" \
	bash scripts/jetson_remote_docker.sh

# --- help --------------------------------------------------------------------

help:
	@echo "capture-pipeline"
	@echo ""
	@echo "Локально (ноутбук):"
	@echo "  make deps                     # Python-зависимости"
	@echo "  make test                     # pytest"
	@echo "  make run-emulate              # RTSP :8554–8557 — отдельный терминал"
	@echo "  make preview-emulate          # HTML :8765 (нужен run-emulate)"
	@echo "  make preview-emulate-sudo     # то же + CAP_NET_ADMIN (netem)"
	@echo ""
	@echo "Плата — Docker Compose (деплой = образ из registry):"
	@echo "  make capture-up               # основной: pull IMAGE_TAG + compose up"
	@echo "  make capture-down             # compose down"
	@echo "  make capture-logs             # docker logs -f"
	@echo "  make capture-status           # docker ps + порты"
	@echo "  make capture-pull             # только pull образа, без рестарта"
	@echo "  make capture-git-sync         # git pull на плате: compose+scripts+configs (код приложения — в образе)"
	@echo "  make capture-build            # запасной: rsync + docker build на плате (если registry недоступен)"
	@echo "  vars: JETSON_SSH=jetson"
	@echo "        DOCKER_CONFIG=$(DUO_CONFIG)    # стенд"
	@echo "        DOCKER_CONFIG=$(ROVER_CONFIG)  # роверы"
	@echo "        IMAGE_TAG=latest"
	@echo "        CPUSET_CPUS=…                  # пусто = все; пример: 7 или 4-7"
	@echo "        SYNC_GIT=1                    # перед up/pull ещё git-sync"
	@echo ""
	@echo "Плата — bootstrap / диагностика (JETSON_SSH=… или вопрос из ssh/config):"
	@echo "  make jetson-auth              # один раз: ssh-copy-id"
	@echo "  make jetson-probe             # SSH→плата, gst к 127.0.0.1:PORTS (нужен capture-up)"
	@echo "  make probe-cams               # ffprobe с ноутбука: RTSP_IP + PORTS (пример PORTS='8586 8587')"
	@echo "  make preview-jetson           # SSH -L RTSP + локальный HTML preview (нужен capture-up)"
	@echo "  make jetson-nav-hz            # Hz NAV на плате ($(NAV_HZ_SECS)s, CONFIG)"

# --- локально ----------------------------------------------------------------

deps:
	$(PYTHON) -m pip install 'numpy>=1.23,<2' PyYAML websockets $(PIP_BREAK_SYSTEM_PACKAGES)

test:
	$(PYTHON) -m pytest -q

run-emulate:
	$(PYTHON) main.py --config $(EMULATE_CONFIG)

preview-emulate:
	@echo "Open preview: http://127.0.0.1:8765/"
	$(PYTHON) scripts/preview_quad.py --preset emulate

# netem нужен CAP_NET_ADMIN. Ctrl+C гасит sudo+python, make не оставляет сирот.
preview-emulate-sudo:
	@echo "Open preview: http://127.0.0.1:8765/"
	@bash -c 'set -euo pipefail; \
		sudo $(PYTHON) scripts/preview_quad.py --preset emulate & \
		pid=$$!; \
		cleanup() { \
			children=$$(pgrep -P $$pid 2>/dev/null || true); \
			kill -TERM $$pid $$children 2>/dev/null || true; \
			for _ in 1 2 3 4 5 6 7 8 9 10; do \
				kill -0 $$pid 2>/dev/null || break; \
				sleep 0.1; \
			done; \
			kill -KILL $$pid $$children 2>/dev/null || true; \
			wait $$pid 2>/dev/null || true; \
		}; \
		trap cleanup INT TERM EXIT; \
		wait $$pid; \
		ec=$$?; \
		trap - EXIT; \
		exit $$ec'

# --- плата: диагностика ------------------------------------------------------

jetson-auth:
	@host=$$(JETSON_SSH="$(JETSON_SSH)" bash scripts/resolve_jetson_ssh.sh); \
	ssh-copy-id -o StrictHostKeyChecking=accept-new "$$host"

# gst на плате к localhost — отделяет сбой драйвера от VPN/маршрута.
jetson-probe:
	$(SSH) 'for p in $(PORTS); do echo --- probe :$$p/live ---; gst-launch-1.0 -q rtspsrc location=rtsp://127.0.0.1:$$p/live protocols=tcp latency=0 ! rtph264depay ! fakesink num-buffers=1 sync=false && echo port $$p OK || echo port $$p FAIL; done'

# ffprobe с ноутбука (нужен маршрут до RTSP_IP, иначе jetson-probe).
probe-cams:
	@for p in $(PORTS); do \
	  echo "--- probe rtsp://$(RTSP_IP):$$p/live ---"; \
	  ffprobe -rtsp_transport tcp -v error -show_entries stream=codec_name,width,height,r_frame_rate \
	    -of default=nw=1 "rtsp://$(RTSP_IP):$$p/live" && echo "port $$p OK" || echo "port $$p FAIL"; \
	done

preview-jetson:
	JETSON_SSH="$(JETSON_SSH)" JETSON_DIR="$(JETSON_DIR)" \
		PREVIEW_PORTS="$(PREVIEW_PORTS)" PREVIEW_LABELS="$(PREVIEW_LABELS)" \
		./scripts/preview_jetson_ssh.sh

jetson-nav-hz:
	$(SSH) 'bash -s -- "$(JETSON_DIR)" "$(CONFIG)" "$(NAV_HZ_SECS)"' \
		< scripts/nav_hz.sh

# --- плата: Docker Compose ---------------------------------------------------

capture-git-sync:
	@$(REMOTE_DOCKER) git-sync

# Только для capture-build (локальная сборка образа на плате).
capture-rsync:
	@host=$$(JETSON_SSH="$(JETSON_SSH)" bash scripts/resolve_jetson_ssh.sh); \
	echo "JETSON_SSH=$$host"; \
	rsync -az --delete -e "ssh $(SSH_OPTIONS)" \
		--exclude '.git/' --exclude '__pycache__/' --exclude 'tests/' \
		--exclude 'snapshots/' --exclude '.pytest_cache/' \
		./ "$$host:capture-pipeline/"; \
	ssh $(SSH_OPTIONS) "$$host" "chmod +x $(JETSON_DIR)/scripts/jetson_docker.sh $(JETSON_DIR)/scripts/jetson_driver_ctl.sh"

capture-build:
	$(MAKE) capture-rsync
	@$(REMOTE_DOCKER) build

capture-pull:
	@$(REMOTE_DOCKER) pull

capture-up:
	@$(REMOTE_DOCKER) up

capture-down:
	@$(REMOTE_DOCKER) down

capture-logs:
	@$(REMOTE_DOCKER) logs

capture-status:
	@$(REMOTE_DOCKER) status
