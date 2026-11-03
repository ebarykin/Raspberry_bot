# 0009. Capture на Pi 5: libcamera и privileged

Статус: Accepted

## Контекст

`capture-pipeline` должен читать OV5647 на Raspberry Pi 5 и отдавать RTSP на `rtsp://127.0.0.1:8554/live` для `webrtc-client-go`. Источник — `libcamerasrc` (NV12 / PiSP), кодирование — программный `x264enc`. ROS 2 в этом контейнере нет.

libcamera на Pi 5 ходит в `/dev/media*`, `/dev/video*`, dma-heap и udev. Перечислить стабильный набор minor-устройств в `docker run --device` нельзя: номера меняются после перезагрузки.

## Решение

Образ `teleop-capture-pipeline` собирается из `services/capture-pipeline/Dockerfile` (Debian 13, без ROS 2). Платформенный YAML — `config/capture-pipeline.yaml`. Контейнер запускается с `--network host` и `--privileged`, плюс монтирование `/dev` и `/run/udev`.

`privileged: true` здесь открывает только CSI-камеру OV5647 через RP1 / libcamera. Cyclone DDS этот контейнер не использует.

Jetson-Dockerfile в subtree (`docker/Dockerfile.jp62`, `docker/Dockerfile.jp72`) на роботе не запускаются. Корневой Compose по-прежнему не заводится: захват стартует отдельным `docker run`.

## Последствия

Без privileged libcamera в контейнере не откроет сенсор. Если позже появится более узкий набор `--device`, этот ADR пересматривают и privileged снимают.

Debian `libcamera-ipa` может не поднять пайплайн PiSP. Тогда с хоста монтируют `/usr/share/libcamera` и соответствующие библиотеки Raspberry Pi OS.
