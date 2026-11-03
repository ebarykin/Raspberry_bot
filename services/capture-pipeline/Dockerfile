# Raspberry Pi 5 (Debian 13 ARM64): libcamerasrc + software x264enc + GstRTSPServer.
# Same image on a laptop with configs/laptop-emulate.yaml (videotestsrc).
# Build on the robot (or with buildx --platform linux/arm64):
#   docker build -f services/capture-pipeline/Dockerfile \
#     -t teleop-capture-pipeline services/capture-pipeline
#
# h264parse lives in gstreamer1.0-plugins-bad. That package also pulls
# Mesa/LLVM, ONNX, TTS, extra codecs. libgstrtspserver Depends on the
# package name, so a dummy .deb satisfies apt; only the two files
# h264parse actually loads are copied from the builder.
FROM debian:13-slim AS gst-h264parse

RUN apt-get update \
    && apt-get download gstreamer1.0-plugins-bad libgstreamer-plugins-bad1.0-0 \
    && for deb in *.deb; do dpkg-deb -x "$deb" /extract; done \
    && mkdir -p /opt/h264parse \
    && cd /extract \
    && find . \( -name 'libgstvideoparsersbad.so' -o -name 'libgstcodecparsers-1.0.so*' \) \
    | while IFS= read -r f; do \
    mkdir -p "/opt/h264parse/$(dirname "$f")" \
    && cp -a "$f" "/opt/h264parse/$f"; \
    done \
    && rm -rf /var/lib/apt/lists/*

FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LIBCAMERA_PIPELINES=rpi/pisp \
    LIBCAMERA_LOG_LEVELS=*:WARNING

RUN apt-get update \
    && mkdir -p /tmp/gst-bad-dummy/DEBIAN \
    && printf '%s\n' \
    'Package: gstreamer1.0-plugins-bad' \
    'Version: 1:99' \
    'Architecture: all' \
    'Multi-Arch: foreign' \
    'Maintainer: teleop-robot' \
    'Description: dummy; h264parse is copied from gst-h264parse' \
    > /tmp/gst-bad-dummy/DEBIAN/control \
    && dpkg-deb -b /tmp/gst-bad-dummy /tmp/gstreamer1.0-plugins-bad-dummy.deb \
    && dpkg -i /tmp/gstreamer1.0-plugins-bad-dummy.deb \
    && rm -rf /tmp/gst-bad-dummy /tmp/gstreamer1.0-plugins-bad-dummy.deb \
    && apt-get install -y --no-install-recommends \
    # Interpreter
    python3 \
    # PyGObject: Gst / GLib / GstRtspServer via GI
    python3-gi \
    # Undistort remap (imported even when cameras[].undistort is false)
    python3-numpy \
    # YAML config loader
    python3-yaml \
    # Control WebSocket :8591 (status / ABR)
    python3-websockets \
    # GI typelib: Gst, GLib
    gir1.2-gstreamer-1.0 \
    # GI typelib: GstRtspServer (also pulls libgstrtspserver)
    gir1.2-gst-rtsp-server-1.0 \
    # GI typelib: GstVideo (ABR force-keyframe)
    gir1.2-gst-plugins-base-1.0 \
    # videotestsrc, videoconvert, videoscale, videorate, tee, identity, appsink/appsrc
    gstreamer1.0-plugins-base \
    # rtph264pay, v4l2src; cairooverlay/jpegenc if NAV or parktronic is enabled
    gstreamer1.0-plugins-good \
    # x264enc
    gstreamer1.0-plugins-ugly \
    # textoverlay (Pango); not in plugins-base on Debian 13
    gstreamer1.0-x \
    # libcamerasrc (Pi CSI / PiSP)
    gstreamer1.0-libcamera \
    # libcamera IPA; not installed by --no-install-recommends
    libcamera-ipa \
    # Fonts for textoverlay HUD
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# h264parse plugin + libgstcodecparsers (not the rest of plugins-bad)
COPY --from=gst-h264parse /opt/h264parse/usr /usr
RUN ldconfig

WORKDIR /app
COPY main.py ./
COPY capture_pipeline ./capture_pipeline
COPY configs ./configs
COPY configs/raspbot.yaml /config/config.yaml
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

EXPOSE 8554 8591
ENTRYPOINT ["/entrypoint.sh"]
CMD ["--config", "/config/config.yaml"]
