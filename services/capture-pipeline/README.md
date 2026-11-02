# capture-pipeline

Мультикамерный low-latency RTSP H.264 publisher:

- **Jetson**: `nvarguscamerasrc`/`v4l2src` → `tee` → `nvv4l2h264enc` → `appsink`
- **Laptop/CI**: `videotestsrc` → `tee` → `x264enc` → `appsink`
- **RTSP**: encoded AU из `appsink` через thread-safe bridge поступают в `appsrc` → `rtph264pay`
- **NAV** (optional, `cameras[].nav_enabled` + `ros_name`): вторая ветка после `tee` → 1280×720@15, один формат `nav.encoding` (`raw` bgr8 или `compressed` JPEG) + `camera_info` (`pipelines/nav.py`)

Очереди teleop/NAV и appsink/appsrc ограничены и leaky: медленный клиент или
perception sink не должен накапливать старые кадры и останавливать capture.

Версия пакета: `0.2.0` (`pyproject.toml` / Helm `appVersion`).

## Структура

```text
capture-pipeline/
  capture_pipeline/
    app.py                 # lifecycle: config → Gst → cameras → WS → shutdown
    config.py              # YAML → frozen Config (+ validation)
    camera_slot.py         # runtime одной камеры (capture, appsrc bridge, RTSP)
    camera_service.py      # набор слотов: start / ABR / restart / stagger
    camera_identity.py     # serial (EEPROM) -> живой Argus sensor_id
    pipelines/             # source + nav + teleop + склейка
      source.py
      nav.py               # NAV: /sensing/{ros_name}/image_raw + compressed + camera_info
      teleop.py            # H.264 → appsink; appsrc RTSP factory
    control_ws.py          # WS :8591 (status + link-quality)
    glib_bridge.py         # вызов кода на GLib MainLoop из других потоков
    log_setup.py           # Python logs + GST_DEBUG до Gst.init
    healthcheck.py         # liveness/readiness через local control WS
    pipeline_recovery.py   # exponential backoff для restart
  configs/                 # готовые YAML (jetson-quad / laptop-emulate)
  scripts/
    jetson_remote_docker.sh  # make capture-*: SSH с ноутбука
    jetson_docker.sh         # compose pull/up на плате
    resolve_jetson_ssh.sh    # выбор Host / JETSON_SSH
    resolve_docker_config.sh # выбор YAML для capture-up
    jetson_ssh.sh            # ssh-обёртка для make jetson-*
    jetson_driver_ctl.sh     # stop native перед capture-up
    set_profile.py           # CLI: один RPC link-quality
    preview_quad.py          # HTML preview :8765 (ffmpeg → JPEG)
    netem_lab.py             # lab-only tc/netem
    preview_jetson_ssh.sh    # SSH -L RTSP + локальный preview
    nav_hz.sh                # замер NAV Hz на плате
    bind_dev_to_sn.sh        # SN GMSL-камер (ручная диагностика)
    static/preview_quad.html
  tests/
  docker/Dockerfile.jp62   # роверы: JetPack 6.2, Ubuntu 22.04, ROS Humble
  docker/Dockerfile.jp72   # робособаки: JetPack 7.2, Ubuntu 24.04, ROS Jazzy
  docker/install.sh        # общая установка; её вызывают оба Dockerfile
  docker/Dockerfile.emulate # x86 videotestsrc, без ROS и без Jetson
  third_party/gstreamer_ros_babel_fish/  # vendored rbfimagesink (AGPL-3.0)
  main.py                  # launcher из корня (без PYTHONPATH)
```

## Режимы

| `source` | Конфиг | Источник |
|----------|--------|----------|
| `jetson` | [`configs/jetson-quad.yaml`](./configs/jetson-quad.yaml) | ZED X / Argus → HW encode |
| `jetson` | [`configs/jetson-duo-one-s.yaml`](./configs/jetson-duo-one-s.yaml) | 1× ZED X One S, 640×400 |
| `emulate` | [`configs/laptop-emulate.yaml`](./configs/laptop-emulate.yaml) | `videotestsrc` (`smpte`, `ball`, …) → `x264enc` |

Capture всегда PLAYING, даже без RTSP-клиента. Это убирает Argus/NVENC preroll
из пути подключения оператора. `rtsp.suspend_mode` управляет только RTSP media;
capture lifetime от него не зависит.

## RTSP URL (Jetson quad)

| Роль (рекомендация) | `id` | URL |
|---------------------|------|-----|
| front | cam0 | `rtsp://<host>:8586/live` |
| rear | cam1 | `rtsp://<host>:8587/live` |
| left | cam2 | `rtsp://<host>:8588/live` |
| right | cam3 | `rtsp://<host>:8589/live` |

**Emulate**: `cam0..cam3` → `:8554`–`:8557` (`smpte` / `ball` / `snow` / `circular`).

## Control WebSocket (`:8591`)

Один сокет, **строго последовательный** RPC (один request → один response).
Поддерживаются `status` и `link-quality`:

```json
→ {"event":"status"}
← {"ok":true,"profile":"good","fps":30,"bitrate_kbps":1200}

→ {"event":"link-quality","payload":{"level":"bad"}}
← {"ok":true,"profile":"bad","fps":18,"bitrate_kbps":450}
```

`level` должен совпадать с ключом `abr.profiles` (имена 1:1, без алиасов).

`ok: true` значит «запрос разобран и политика применена», а не «профиль
обязательно сменился». Если уровень уже активен или не выдержан
`abr.min_dwell_s`, ответ всё равно `ok: true`, но `profile` остаётся прежним.

Lab-only: `payload.force: true` пропускает dwell (preview UI так и делает).
Production streamer **не** должен слать `force`.

Toggle камер и per-camera bitrate не входят в этот протокол.

По умолчанию в Python bind `127.0.0.1` **без auth** (laptop emulate).
На Jetson (`configs/jetson-quad.yaml`, Helm chart) bind **`0.0.0.0`**: стример —
отдельный процесс и ходит на LAN IP камеры, как на RTSP `:8586–8589`.
Иначе `ws://192.168.x.x:8591` → `ECONNREFUSED`, ABR в UI «—».
Не публикуйте `:8591` в интернет; на борту это тот же сегмент, что RTSP.

```bash
python3 scripts/set_profile.py critical
python3 scripts/set_profile.py --level degraded
```

### ABR-профили

| Профиль | FPS | Bitrate |
|---------|-----|---------|
| `good` | 30 | 1200 kbit/s |
| `normal` | 30 | 800 kbit/s |
| `degraded` | 24 | 600 kbit/s |
| `bad` | 18 | 450 kbit/s |
| `critical` | 12 | 300 kbit/s |

`warn` на gst не принимается: UI нормализует старый 3-level payload сам.

Runtime bitrate и FPS берутся только из `abr.profiles`. Секция `encoder`
содержит HW tuning. При `vbv_size: 0` размер VBV рассчитывается как
`bitrate × vbv_horizon_s`; GOP — как `fps × gop_s`. При смене ABR обновляются
bitrate, peak (120%), VBV, GOP и принудительно запрашивается IDR.

## Локальный запуск (ноутбук)

```bash
cd capture-pipeline
make deps
make run-emulate
# RTSP: :8554–8557  control WS :8591
# в другом терминале: make preview-emulate  → http://127.0.0.1:8765/
```

### Preview 2×2 (HTML / React)

Браузер не играет RTSP — нужен мост. `scripts/preview_quad.py` читает N портов
фоновым ffmpeg и отдаёт JPEG; React-страница поллит `/jpeg/<i>` (~8 fps).
Число плиток = `/api/cameras`. Статусы: **LIVE** / **STALE** / **NO SIGNAL**
(нет кадров с RTSP — не «камера выключена»).

На странице есть **ABR simulate**: кнопки `good…critical` → `POST /api/link-quality`
→ control WS драйвера (`link-quality` + `force: true`), плюс лог request/response.
Нужны `websockets` и запущенный zed-driver с control WS `:8591`.

Для preview **крайне желателен sudo** (`CAP_NET_ADMIN`): без него сетка камер и ABR
работают, а кнопки NETEM (delay/loss/rate) — нет. Предпочитай
`make preview-emulate-sudo`. После старта в терминале будет ссылка
`http://127.0.0.1:8765/` — её можно открыть кликом.

**Ноут (emulate):**

```bash
make run-emulate              # терминал 1
make preview-emulate-sudo     # терминал 2 → http://127.0.0.1:8765/
# без sudo (только камеры + ABR, без NETEM):
# make preview-emulate
```

**NETEM (lab tc)** на странице preview требует `CAP_NET_ADMIN`:

```bash
make preview-emulate-sudo   # камеры + ABR + кнопки NETEM (delay/loss/rate на lo/UDP)
# Open preview: http://127.0.0.1:8765/
```

NETEM режет **UDP egress** (имитация uplink). TCP (HTTP preview, SSH, control WS)
не шейпится. Это **не** часть контракта control WS.

Если `:8765` занят (старый preview): `sudo fuser -k 8765/tcp`, затем снова
`make preview-emulate-sudo`. Ctrl+C в этой цели корректно гасит sudo-процесс.

**Jetson по SSH** (preview на роботе, браузер на ноуте):

```bash
# Один раз или после изменения preview-скриптов:
make jetson-sync JETSON_SSH=nvidia@10.7.12.1

# Драйвер уже должен слушать 8586–8589 на dog02:
make preview-jetson \
  JETSON_SSH=nvidia@10.7.12.1 \
  PREVIEW_PORTS='8586 8587 8588 8589' \
  PREVIEW_LABELS='front rear left right'

# После запуска открыть http://127.0.0.1:8765/
```

Произвольное число камер:

```bash
python3 scripts/preview_quad.py --ports 8554,8555 --labels front,rear
```

Нужен `ffmpeg` в PATH (и на Jetson, и локально для emulate preview).

## Jetson

Локально на самой плате:

```bash
make deps && make check && make run
```

`make run` и удалённые `jetson-run*` по умолчанию используют
`configs/jetson-quad.yaml`. Для стенда 1× ZED X One S используйте
`make run-duo` или явно `CONFIG=configs/jetson-duo-one-s.yaml`.

### ZED Link Duo на Orin NX (L4T R39.2)

Стенд `Host jetson` — Orin NX и карта **ZED Link Duo**, не Quad. Без этого
`.deb` Argus не создаёт `/dev/tegra_camera_ctrl` и отвечает `No cameras available`.
Пакет: `stereolabs-zedlink-duo_1.4.4-LI-MAX96712-L4T39.2.0_arm64.deb`
(ядро не RT; Quad `-SL-MAX96712`, Mono и `zedbox-*` не ставить).

```bash
# На плате (после scp .deb):
sudo dpkg -i stereolabs-zedlink-duo_1.4.4-LI-MAX96712-L4T39.2.0_arm64.deb
sudo reboot
```

Пакет прописывает overlay `zedlink-duo` в `/boot/extlinux/extlinux.conf`.
Картинка появится только после reboot. ZED SDK не нужен.

### Развёртывание на dog02 с ноутбука

Все команды выполняются в локальном checkout. `jetson-run-bg` сам безопасно
останавливает предыдущий ручной экземпляр из `~/capture-pipeline`, ждёт
освобождения портов и проверяет, что новый процесс не завершился сразу.

```bash
# Перейти в локальный репозиторий.
cd /path/to/capture-pipeline

# Выполнить один раз: установить SSH-ключ для входа без повторных паролей.
make jetson-auth JETSON_SSH=nvidia@10.7.12.1

# Загрузить код, конфиги, служебные скрипты и Python-зависимости.
make jetson-sync JETSON_SSH=nvidia@10.7.12.1

# Остановить прежний ручной процесс и запустить quad-конфигурацию в фоне.
make jetson-run-bg \
  JETSON_SSH=nvidia@10.7.12.1 \
  CONFIG=configs/jetson-quad.yaml

# Проверить PID и слушающие RTSP/WS-порты.
make jetson-status \
  JETSON_SSH=nvidia@10.7.12.1 \
  CONFIG=configs/jetson-quad.yaml

# Получить реальные данные со всех четырёх RTSP-потоков на самой dog02.
make jetson-probe \
  JETSON_SSH=nvidia@10.7.12.1 \
  PORTS='8586 8587 8588 8589'
```

Повторное обновление выполняется теми же `jetson-sync` и `jetson-run-bg`;
отдельный `jetson-stop` перед ними не нужен. Только остановка без запуска:

```bash
make jetson-stop JETSON_SSH=nvidia@10.7.12.1
```

Посмотреть лог:

```bash
ssh nvidia@10.7.12.1 'tail -f /tmp/capture_pipeline.log'
```

### Запуск из уже открытой SSH-сессии dog02

После `jetson-sync` можно управлять процессом непосредственно на плате:

```bash
cd ~/capture-pipeline

# Остановить старый процесс, дождаться портов и запустить quad в фоне.
bash scripts/jetson_driver_ctl.sh run-bg \
  "$HOME/capture-pipeline" \
  configs/jetson-quad.yaml

# Показать проверенный PID и слушающие порты.
bash scripts/jetson_driver_ctl.sh status \
  "$HOME/capture-pipeline" \
  configs/jetson-quad.yaml

# Только остановить ручной экземпляр.
bash scripts/jetson_driver_ctl.sh stop \
  "$HOME/capture-pipeline" \
  configs/jetson-quad.yaml
```

Скрипт проверяет рабочий каталог и аргументы процесса перед отправкой сигнала,
поэтому устаревший PID-файл не приводит к остановке постороннего процесса.

### Always-on capture

Capture стартует сам (`parse_launch` → PLAYING) и постоянно кодирует H.264 в
`appsink`. При создании RTSP media его `appsrc` регистрируется в bridge,
получает поток начиная с IDR и удаляется при `unprepared`. RTSP PLAY не
перезапускает камеру или NVENC.

- `capture.stagger_ms: 200` — пауза между PLAYING соседних слотов
- `rtsp.suspend_mode` — `none` / `pause` / `reset` только для RTSP media
- `cameras[].source_type` — optional (`nvarguscamerasrc` по умолчанию на jetson, `v4l2src` + `device` для YUV SG3S)
- `cameras[].nav_enabled` — optional; по умолчанию false (teleop-only). `true` требует уникальный `cameras[].ros_name`.
- `cameras[].parktronic` — optional; HUD только teleop этой камеры (не NAV).
  `false` / нет ключа = выкл; `true` = дефолты; mapping = геометрия. В mapping
  `enabled: false` выключает HUD, не стирая числа (`enabled` по умолчанию true).
- `cameras[].undistort` — optional; Brown remap 640×H до HUD, только teleop.
  Тот же контракт, что parktronic; в mapping: `k1` / `k2` / `crop`.

Проверка RTSP (после `camN capture playing` в логе):

С ноутбука по VPN (`JETSON_RTSP_HOST`; ниже пример для `10.7.12.1`):

```bash
for p in 8586 8587 8588 8589; do
  echo "===== :$p ====="
  timeout 8 ffprobe -rtsp_transport tcp -v error \
    -show_entries stream=codec_name,width,height,r_frame_rate \
    -of default=nw=1 \
    rtsp://10.7.12.1:$p/live \
    && echo OK || echo FAIL
done
```

`timeout 8` — утилита coreutils, не опция FFmpeg. На одном порту без лимита:

```bash
ffprobe -rtsp_transport tcp -v error \
  -show_entries stream=codec_name,width,height,r_frame_rate \
  -of default=nw=1 \
  rtsp://10.7.12.1:8586/live
```

На борту (loopback):

```bash
for p in 8586 8587 8588 8589; do
  echo "===== :$p ====="
  timeout 8 ffprobe -rtsp_transport tcp -v error \
    -show_entries stream=codec_name,width,height,r_frame_rate \
    -of default=nw=1 \
    rtsp://127.0.0.1:$p/live \
    && echo OK || echo FAIL
done
```

### Аппаратный просмотр на Jetson

Правильная low-latency команда для локального просмотра использует NVDEC
(`nvv4l2decoder`) и NVMM-compatible renderer:

```bash
gst-launch-1.0 -e \
  rtspsrc location=rtsp://127.0.0.1:8586/live \
    protocols=tcp \
    latency=0 \
    buffer-mode=none \
    drop-on-latency=true \
  ! rtph264depay \
  ! h264parse \
  ! nvv4l2decoder \
    enable-max-performance=true \
    disable-dpb=true \
    num-extra-surfaces=0 \
  ! nv3dsink \
    sync=false \
    async=false \
    qos=false
```

`disable-dpb=true` безопасен для текущего потока без B-frames. Если команда
запущена из SSH-сессии, может потребоваться `export DISPLAY=:0`.

Quad должен вернуть `codec_name=h264`, `width=640`, `height=480`, `OK`.
One S использует 640×400 для сохранения нативного 16:10.

Не использовать `-stimeout` / `-timeout` у ffprobe 6.x: `-stimeout` часто «Option not found», `-timeout` включает listen и даёт `Address already in use`.

Все четыре потока должны держаться >30 с без лавины `GST_ARGUS Cleaning up` в логе zed. На Jetson без `DISPLAY` `ffplay` не открыть (`No available video device`).

### Lifecycle / shutdown

При SIGINT/SIGTERM:

1. останавливается control WS (чтобы не было in-flight ABR);
2. `CameraService.shutdown()` снимает RTSP mount, останавливает capture и таймеры restart/stagger.

Так порты и Argus освобождаются без `kill -9`.

### Recovery

При ERROR/EOS на capture:

- рестартится **только** этот слот (другие не трогаем);
- backoff: `pipeline.restart_backoff_ms` → ×2 до `restart_max_backoff_ms`;
- повторные ошибки объединяются, последняя причина сохраняется.

RTSP media имеет отдельный bus watch: при ошибке payloader/appsrc media
unprepare-ится и следующий клиент получает новый media pipeline с IDR.

### Залипшие сессии Argus

`nvargus-daemon` живёт на хосте и держит состояние CaptureSession по клиентам.
Если предыдущий pod умер без graceful shutdown (OOM, `--force`, SIGKILL по
таймауту пробы), демон считает сессии занятыми, и часть камер падает с
`Failed to create CaptureSession` → EOS на каждой попытке, пока демон не
перезапустят. Изнутри пода это не лечится.

Поэтому:

- `terminationGracePeriodSeconds: 20` — запас, чтобы SIGTERM-путь (control WS →
  `unprepare` RTSP-медиа → пайплайны в NULL) всегда доходил до конца. Сам
  релиз 3-8мс на камеру, штатный выход происходит сразу по завершении.
- Init-контейнер `reset-argus-daemon` (по умолчанию выключен, пример в
  `initContainers` в values) перезапускает демон на хосте **перед**
  `wait-argus-socket`, так что каждый pod начинает с чистого состояния
  независимо от того, как умер предыдущий. Делается через `nsenter --target 1`
  в namespace'ы хоста, поэтому вместе с ним нужен `hostPID: true`.
- `startupProbe` проверяет только ответ control WS. Раньше `--require-playing`
  убивал контейнер, пока ни одна камера не в `playing`, и kubelet крутил под
  без повторного init. `readinessProbe` по-прежнему требует хотя бы одну
  `playing`, поэтому без кадра под не Ready.
- `livenessProbe` — тот же ответ WS, раз в 30 с, три пропуска подряд
  (около 90 с тишины) до рестарта контейнера. Кадры она не проверяет.
- `Cannot create camera provider` / `Failed to create CameraProvider` — демон
  не принял RPC. Повтор слота это не лечит. Драйвер останавливает камеры,
  через `nsenter` делает `systemctl restart nvargus-daemon` и ждёт провайдер.
  Больше трёх таких рестартов за жизнь процесса — выход с кодом 1.
- Init `wait-argus-socket` пишет в лог inode сокета и каждую попытку probe.
  Фиксированной паузы после сокета нет: провайдер пробуется сразу и каждые 2 с,
  пока демон не ответит или не истечёт `--timeout` (120 с).

Включать `reset-argus-daemon` не стоит там, где Argus делится с другим
потребителем (перезапуск демона рвёт камеры и ему) или где политики кластера
запрещают `hostPID`.

### Camera identity: sensor_id vs serial

`cameras[].sensor_id` — это индекс, под которым Argus видит камеру на
конкретной плате (`nvarguscamerasrc sensor-id=N`). Он никак не связан с
физическим модулем: при другой разводке GMSL/I2C или другом devicetree-оверлее
тот же физический модуль может оказаться под другим `sensor_id`, поэтому номер
приходится подбирать руками на каждом роботе (см. `configs/jetson-quad.yaml`,
`jetson-delta.yaml`, `jetson-duo-one-s.yaml` — у каждой платы своя раскладка).

Чтобы не завязываться на этот хрупкий индекс, камеру можно привязать к её
физическому серийнику:

```yaml
cameras:
  - id: cam0
    serial: 123456789   # читается с EEPROM модуля, sensor_id не нужен
    rtsp_port: 8586
    mount: /live
```

На старте `CameraService.start_configured()` (если хоть у одной включённой
камеры задан `serial`) один раз сканирует все подключённые модули
(`capture_pipeline/camera_identity.py`): читает serial с EEPROM через
V4L2-контрол `zed_eeprom_data` и берёт `sensor_id` по VI-каналу
(`platform:tegra-capture-vi:N` в `v4l2-ctl --list-devices`). Argus нумерует
только реально заprobленные камеры, `0..N-1` в порядке VI-каналов, а каналы
бывают с дырами (одна камера во втором порту ZED Link — vi-channel 1, но
sensor-id 0). Поэтому `sensor_id` — позиция канала среди найденных, а не его
номер. Найденный `sensor_id`
подставляется в конфиг камеры перед запуском capture — значение `sensor_id`
из YAML в этом случае игнорируется и может быть не указано вовсе.

Нумерация `moduleN` в devicetree (`tegra-camera-platform/modules`) для этого
не подходит: там объявлены слоты под все варианты плат (на AGX Orin + ZED Link
Quad их 34, включая дубли `module0` / `module00`), так что позиция физических
камер в этом списке — 16..20 — Argus-ом не принимается. По этой же причине
вендорский `bind_dev_to_sn.sh` на такой плате выдаёт непригодные `ID` (18..23).

Если `serial` задан, но не нашёлся среди подключённых модулей (отвалившийся
кабель, не читается EEPROM) — **только эта** камера остаётся в
`state: "error"` (видно в `{"event":"status"}` control WS), остальные
стартуют как обычно; откат на `sensor_id`-плейсхолдер не происходит.

Узнать serial подключённых камер (например, чтобы прописать его в конфиг):

```bash
python3 -m capture_pipeline.camera_identity
# sensor_id=0 serial=...
# sensor_id=1 serial=...
```

### Sensor fps vs ABR fps

Argus захватывает на `video.fps` (обычно 30). `videorate max-rate` задаётся
только при сборке пайплайна: `nvv4l2h264enc` отвергает повторный setcaps с
другим framerate и отдаёт пустые буферы. Дальше ABR снижает частоту дропом
кадров до энкодера и меняет bitrate / GOP свойствами. Нагрузка Argus на
`critical` не падает — осознанный tradeoff latency/recovery vs thermal.

### Логирование

В YAML (`logging.level`, `logging.gst_debug`). Шипованные jetson-конфиги
`gst_debug` **не задают** — берётся `DEFAULT_GST_DEBUG` из
[`config.py`](capture_pipeline/config.py): глобально WARNING, но заведомо
безвредный спам приглушён по категориям — отсутствующие в контейнере
опциональные плагины (`GST_PLUGIN_LOADING`, `vadisplay`, `nvenc`, `nvcodec`,
`ladspa`), квирки `nvv4l2h264enc` (`v4l2`, `v4l2bufferpool`), `appsrc`
(`Dropping old item buffer` на каждый GOP для залипшего клиента) и
`rtspclient` (`no factory for path` от клиентов, пришедших до появления
mount — состояние камеры всё равно видно в `status`).

Задать свой фильтр можно, прописав `logging.gst_debug` в YAML — тогда
дефолт не применяется. `GST_DEBUG` выставляется **до** `Gst.init`.

Логи библиотеки `websockets` приглушены до WARNING: k8s-пробы ходят в
`status` каждые 2 секунды, и на INFO это заливало лог сообщениями
`connection open` / `client connected`. Обрыв рукопожатия
(`opening handshake failed`) пишется одной строкой WARNING, без трейсбека.

NVIDIA печатает баннеры прямо в stdout, мимо `GST_DEBUG`. Сторож
`ArgusStderrWatch` их отбрасывает: `Opening in BLOCKING MODE`, каталог режимов
`GST_ARGUS`, `NvMMLiteOpen` / `NvVideo` / `H264:` / `NVMEDIA`, предупреждения
`gst-plugin-scanner` про отсутствующие опциональные библиотеки и
`lo is not multicast-capable`. В лог проходят отказы Argus (`(Argus) Error`,
`Cannot create camera provider`, `Failed to create CameraProvider`,
`Failed to create CaptureSession`, `Invalid camera`, `GST_ARGUS Cleaning up`):
на bus пайплайна их нет.

Примеры строк:

```text
INFO [zed] source=jetson sensor 1920x1200@30 -> out 640x480@30 profile=good bitrate_kbps=1200 fps=30 cameras=4 transport=appsink-appsrc
INFO [cam0] ready rtsp://0.0.0.0:8586/live (sensor=0 flip=0)
INFO [cam0] capture playing appsink→appsrc
INFO [capture_pipeline.control_ws] control WebSocket on ws://0.0.0.0:8591
INFO [cam0] rtsp clients 0→1
WARNING [cam2] pipeline error: ... restart in 2.0s (attempt 2)
INFO [capture_pipeline.camera_service] camera service shutdown complete
```

## Зависимости

**Общее:** `python3-gi`, `gir1.2-gst-rtsp-server-1.0`, NumPy, PyYAML, websockets.
OpenCV необязателен: он ускоряет включённую коррекцию искажений; без него
используется существенно более медленная NumPy-реализация.

```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-rtsp-server-1.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly
make deps
```

На ноуте для emulate нужен `x264enc` (обычно `gstreamer1.0-plugins-ugly`)
и `textoverlay`.

Два образа Jetson. Шаги установки общие (`docker/install.sh`), платформа зашита в файл:
Ubuntu образа совпадает с Ubuntu платы, иначе смонтированные библиотеки Tegra не линкуются.

| Куда | JetPack | Файл | База | ROS |
|------|---------|------|------|-----|
| роверы | 6.2, L4T R36 | `docker/Dockerfile.jp62` | `l4t-base:r36.2.0` (Ubuntu 22.04; новее NVIDIA не публикует) | Humble |
| робособаки | 7.2, L4T R39.2 | `docker/Dockerfile.jp72` | `ubuntu:24.04` | Jazzy |

HW-плагины (`nvarguscamerasrc`, `nvv4l2h264enc`) в эти образы не ставятся.
Helm и `docker/compose.yaml` монтируют `/dev` и tegra с хоста.
`rbfimagesink` собирается в образе из [`third_party/gstreamer_ros_babel_fish`](./third_party/gstreamer_ros_babel_fish)
(`COPY` + colcon, без `git clone`).

CI публикует `last-mile/drivers/capture-pipeline` (6.2, роверы)
и `last-mile/drivers/capture-pipeline-jp72` (7.2) в `cr.selcloud.ru/sandbox`.
Helm-чарт — целевой запуск в кластере и по умолчанию тянет 7.2.
Стенд без Kubernetes: тот же образ через Compose, без `docker build` на плате.

На плате один раз: `docker login cr.selcloud.ru` и `git clone` репозитория
(старый rsync без `.git` заменить clone'ом).

- **Образ (registry)** — код приложения (`capture_pipeline/`, entrypoint).
- **Git на плате** — `docker/compose.yaml`, `scripts/jetson_docker.sh`, `configs/*.yaml`
  (YAML монтируется в контейнер; compose/скрипт на хосте запускают контейнер).
- `make capture-git-sync` / `SYNC_GIT=1` обновляет хостовую обвязку, не образ.
- Образ: `capture-up` / `capture-pull` (`:latest` = последний зелёный `develop`).

Опционально на плате: `cp docker/.env.example docker/.env` (CONFIG_HOST по умолчанию).

```bash
# спросит JETSON_SSH (если не задан) и DOCKER_CONFIG (или CONFIG_HOST из docker/.env на плате)
make capture-up
# явно sandbox + стенд Duo:
make capture-up JETSON_SSH=jetson DOCKER_CONFIG=configs/jetson-duo-one-s.yaml
# ровер:
make capture-up JETSON_SSH=nvidia@10.7.12.1 DOCKER_CONFIG=configs/jetson-delta.yaml

# после merge compose/скриптов в develop:
make capture-up JETSON_SSH=jetson SYNC_GIT=1 DOCKER_CONFIG=configs/jetson-duo-one-s.yaml
```

На плате (предпочтительно через скрипт — подхватит JetPack, Argus, docker/.env):

```bash
bash scripts/jetson_docker.sh up configs/jetson-duo-one-s.yaml
# вручную compose:
docker compose --project-directory . -f docker/compose.yaml \
  --env-file docker/.env pull
docker compose --project-directory . -f docker/compose.yaml \
  --env-file docker/.env up -d
```

Запасной путь без registry — сборка на плате:

```bash
make capture-build JETPACK=6.2
IMAGE=capture-pipeline:jp72 PULL_POLICY=never bash scripts/jetson_docker.sh up configs/jetson-duo-one-s.yaml
```

L4T на плате: R36 → образ 6.2, R39 → 7.2. В Helm для ровера 6.2
переопределяют `image.repository` на репозиторий без `-jp72`.

Чарт построен на `ros-chart-starter`. Helm создаёт Deployment со стратегией `Recreate`.
StatefulSet не подходит: при `OrderedReady` он не заменяет не-Ready pod,
и драйвер, сломанный конфигом, не чинится новым деплоем. Service выключен
(`service.enabled: false`): pod в `hostNetwork`, клиенты ходят на RTSP и control WS
по адресу хоста. `startupProbe` и `livenessProbe` проверяют ответ control WS.
`readinessProbe` требует хотя бы одну камеру в `playing` и не перезапускает pod.
Если меняется порт control WS, пробы в values нужно переопределить вместе с конфигом.

Шаблоны чарта совпадают со стартером. Init-контейнеры Argus заданы в `initContainers`
в values: незаданные поля (образ, env, mounts) берутся у основного контейнера, поэтому
переопределение `volumeMounts` (как на dog-002) init подхватывает само.
Конфига по умолчанию в чарте нет: его передают через `config.override` + `config.values`
(так делает `dogster`) или `config.lookupGlobal`.

### Helm operations

```bash
helm lint chart
helm template capture-pipeline chart
helm upgrade --install capture-pipeline chart --namespace <namespace>
helm history capture-pipeline --namespace <namespace>
helm rollback capture-pipeline <revision> --namespace <namespace>
```

При зависании init container сначала проверить Argus socket и daemon на host.
При падении одной камеры смотреть её `state`, `last_error` и `restart_attempts`
через control WS; pod restart нужен только если per-slot recovery не помогает.

## Конфиг

- `source`: `jetson` | `emulate`
- `rtsp`: `host`, `suspend_mode`; transport всегда appsink/appsrc
- `capture`: `stagger_ms`; capture всегда стационарный
- `pipeline`: `restart_backoff_ms`, `restart_max_backoff_ms` (per-camera recovery)
- `nav.sink_type`: optional `auto` | `rbfimagesink` | `fakesink`
- `nav.encoding`: optional `raw` | `compressed` (default `compressed`). Один топик, не оба.
- `nav.width` / `height` / `fps` / `jpeg_quality`: optional, default 1280×720@15, JPEG 80
  (production Helm/quad: JPEG 100).
  Топик / `frame_id` / node-name считаются из `cameras[].ros_name`, не из YAML.
- `cameras[]`: как раньше + optional `source_type`, `device`, `pixel_format`,
  `ros_name`, `nav_enabled`, `parktronic`, `undistort`
- `logging`: `level`, `gst_debug` (GStreamer stderr; задаётся до `Gst.init`)
- `abr.profiles` обязателен; `abr.default_profile` / `abr.min_dwell_s` optional.
  `level` в control WS = ключ профиля (без `abr.level_map`)
- `control_ws`: `enabled` / `host` / `port`. Jetson + enabled → `host` обязателен
  (loopback ломает LAN-стример). Emulate default `127.0.0.1:8591`
- `video.width` / `height` / `fps` обязательны. Jetson также требует
  `sensor_width` / `sensor_height`
- `encoder.*`: HW tuning; runtime bitrate/fps/GOP — из ABR.
  Jetson требует `control_rate` / `preset_level` / `gop_s` / `vbv_horizon_s`.
  `qp_range` / `vbv_size` (0 = follow bitrate) — иначе пол ~4 Mbit/s
- legacy `control:`, `abr.host/port/enabled`, `abr.level_map`, `rtsp.mode`,
  `capture.always_on/suspend_mode` и encoder bitrate/iframe keys дают ошибку загрузки

## Production checklist

- `overlay.clock: false`: software overlay нужен только для glass-to-glass замеров.
- Ограничить `:8591` сетевой политикой/VLAN: control WS не имеет auth/TLS.
- Проверить все RTSP-порты и reconnect после старта.
- Провести длительный четырёхкамерный soak-тест с ABR и потерями сети.
- Настроить внешние метрики/алерты по status, restart count и отсутствию кадров:
  встроенного Prometheus endpoint пока нет.
