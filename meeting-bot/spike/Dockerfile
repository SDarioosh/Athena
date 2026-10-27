# syntax=docker/dockerfile:1
# Athena Zoom bot, Phase 0 spike image (throwaway, not built in CI).
#
# The Zoom Meeting SDK for Linux is proprietary and never in git. It comes in as a
# separate BuildKit build context, read from the owner's folder at build time:
#
#   docker buildx build --load -t athena-meeting-spike \
#     --build-context zoomsdk="$HOME/zoom-sdk" meeting-bot/spike
#
# The SDK ends up inside this local image, so never push the image to a registry.
# Run (one meeting, then the container is gone):
#
#   docker run --rm --init -e CONVO_ZOOM_SDK_KEY -e CONVO_ZOOM_SDK_SECRET \
#     -e ATHENA_SPIKE_PULSE=0 -v "$PWD/out:/out" athena-meeting-spike \
#     -- --meeting-id 81234567890 --passcode xxxxxx --minutes 30 --out /out
#
# Add --python after the image name to run the Python-binding variant (image built with
# --build-arg WITH_PYTHON=1).

ARG UBUNTU=ubuntu:24.04
# Libraries the SDK and its bundled Qt load (from Zoom's Linux samples, without their build
# tools, OpenCV and debugger). The build stage needs them too: the linker resolves the
# SDK's own dependencies. The ldd check at the end names anything still missing.
ARG SDK_LIBS="libglib2.0-0t64 libdbus-1-3 libgbm1 libdrm2 libgl1 libegl1 \
  libx11-6 libx11-xcb1 libxcb1 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
  libxcb-shape0 libxcb-shm0 libxcb-xfixes0 libxcb-xtest0 libxfixes3 libxkbcommon0 \
  libfontconfig1 libfreetype6 libatomic1 libssl3t64 libasound2t64"

FROM ${UBUNTU} AS build
ARG DEBIAN_FRONTEND=noninteractive
ARG SDK_LIBS
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config libglib2.0-dev ca-certificates $SDK_LIBS \
    && rm -rf /var/lib/apt/lists/*
COPY --from=zoomsdk . /opt/zoomsdk
COPY cpp /src/cpp
RUN test -f /opt/zoomsdk/h/zoom_sdk.h \
      || { echo "zoomsdk context is not the unpacked Meeting SDK (no h/zoom_sdk.h)"; exit 1; } \
    && cmake -S /src/cpp -B /src/cpp/build -DZOOM_SDK_DIR=/opt/zoomsdk \
    && cmake --build /src/cpp/build -j"$(nproc)"

FROM ${UBUNTU} AS run
ARG DEBIAN_FRONTEND=noninteractive
ARG WITH_PULSE=1
ARG WITH_PYTHON=0
ARG SDK_LIBS
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 $SDK_LIBS \
    && if [ "$WITH_PULSE" = 1 ]; then \
         apt-get install -y --no-install-recommends pulseaudio pulseaudio-utils; fi \
    && if [ "$WITH_PYTHON" = 1 ]; then \
         apt-get install -y --no-install-recommends python3-gi python3-pip python3-dev \
           build-essential cmake git; fi \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m athena
COPY --from=zoomsdk . /opt/zoomsdk
COPY --from=build /src/cpp/build/athena-meeting-spike /opt/spike/cpp/build/athena-meeting-spike
COPY --from=build /src/cpp/build/sdklib /src/cpp/build/sdklib
COPY sign_jwt.py run.sh /opt/spike/
COPY python /opt/spike/python
# Python binding built from source against the same SDK (not the PyPI wheel, which
# bundles its own copy of libmeetingsdk). UNVERIFIED: check the binding's README for its
# current build steps; ZOOM_BINDING_REF pins the commit you tested.
ARG ZOOM_BINDING_REF=main
RUN if [ "$WITH_PYTHON" = 1 ]; then \
      git clone https://github.com/noah-duncan/py-zoom-meeting-sdk /tmp/pyzoom \
      && git -C /tmp/pyzoom checkout "$ZOOM_BINDING_REF" \
      && cp -a /opt/zoomsdk/. /tmp/pyzoom/src/zoomsdk/ \
      && pip3 install --break-system-packages /tmp/pyzoom \
      && rm -rf /tmp/pyzoom; fi
# Where the SDK and its bundled Qt are found at run time (the Python binding loads the SDK
# itself, so the binary's RPATH alone isn't enough).
ENV LD_LIBRARY_PATH=/opt/zoomsdk:/src/cpp/build/sdklib:/opt/zoomsdk/qt_libs:/opt/zoomsdk/qt_libs/Qt/lib
# Fail the build if the SDK needs a library this image lacks, seen exactly as a run sees it.
RUN missing="$(ldd /opt/spike/cpp/build/athena-meeting-spike | grep 'not found' || true)"; \
    if [ -n "$missing" ]; then echo "missing libraries:"; echo "$missing"; exit 1; fi
USER athena
WORKDIR /home/athena
ENTRYPOINT ["/opt/spike/run.sh"]
