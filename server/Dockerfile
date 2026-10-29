# odysseus-sound server image: `convo-server serve` and `convo-server mcp --http`.
# Built and run by compose.yml (README "Docker"). Settings come from server/.env at run
# time; nothing secret is baked in.
#
# Stages: `server` is the image (the default target, the last stage, is exactly it).
# `meeting` adds the Zoom meeting bot on top; only compose.meeting.yml builds it, for
# sound-api, when BARVIS_SOUND_ZOOM_SDK_DIR is set (README "Meeting bot in Docker").
# Without that target BuildKit skips the bot stages and needs no SDK.
ARG BASE=python:3.12-slim
# Libraries the Zoom Meeting SDK and its bundled Qt load (meeting-bot/spike/Dockerfile,
# Debian names). The build stage needs them too: the linker resolves the SDK's own
# dependencies. The ldd check in `meeting` names anything still missing.
ARG SDK_LIBS="libglib2.0-0t64 libdbus-1-3 libgbm1 libdrm2 libgl1 libegl1 \
  libx11-6 libx11-xcb1 libxcb1 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
  libxcb-shape0 libxcb-shm0 libxcb-xfixes0 libxcb-xtest0 libxfixes3 libxkbcommon0 \
  libfontconfig1 libfreetype6 libatomic1 libssl3t64 libasound2t64"
# Where the bot lives in the image: the binary, its start script and the SDK (zoomsdk/).
# The build uses the same paths, so the binary's RPATH is right at run time.
ARG BOT_DIR=/opt/athena-meeting-bot

FROM ${BASE} AS server

# vad,diarization by default; add pyannote for CONVO_DIARIZER=pyannote.
ARG EXTRAS=vad,diarization
# CPU torch: ECAPA speaker embeddings and Silero VAD run fine on CPU, and the CUDA
# wheels would add ~3 GB. Whisper gets the GPU (docker/whisper.Dockerfile).
# compose.gpu.yml passes a CUDA index instead, for pyannote on the GPU.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change doesn't reinstall torch. torchcodec (pyannote
# reads audio with it) comes from TORCH_INDEX too: PyPI's is built for PyPI's torch
# (CUDA 13) and fails to load next to this one. The constraints keep these builds when
# the extras are installed.
COPY pyproject.toml ./
RUN mkdir athena && touch athena/__init__.py \
    && pip install --no-cache-dir --index-url "$TORCH_INDEX" torch torchaudio torchcodec \
    && pip freeze | grep -E '^(torch|torchaudio|torchcodec)==' > /tmp/torch-pins.txt \
    && pip install --no-cache-dir -c /tmp/torch-pins.txt ".[$EXTRAS]" \
    && pip uninstall -y -q athena-server && rm -rf athena build

# convo_server: the package's old name, an alias of athena for old commands and imports.
COPY athena ./athena
COPY convo_server ./convo_server
COPY scripts ./scripts
RUN pip install --no-cache-dir --no-deps . && rm -rf build

COPY docker/entrypoint.sh /usr/local/bin/convo-entrypoint
# The container runs as the host user (compose.yml `user:`), so HOME must be writable
# by anyone; models and the Hugging Face cache live in the data volume.
ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    CONVO_DATA_DIR=/data \
    HF_HOME=/data/models/huggingface

ENTRYPOINT ["convo-entrypoint"]
CMD ["serve"]

# ---------------------------------------------------------------- Zoom meeting bot
# Builds meeting-bot/ against the SDK. Both come in as BuildKit build contexts
# (compose.meeting.yml: `meetingbot` = the repo's meeting-bot/, `zoomsdk` = the unpacked
# SDK), never from git and never from server/.
FROM ${BASE} AS meeting-bot-build
ARG SDK_LIBS
ARG BOT_DIR
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential cmake pkg-config libglib2.0-dev libcurl4-openssl-dev \
       libopus-dev libopusenc-dev libogg-dev $SDK_LIBS \
    && rm -rf /var/lib/apt/lists/*
COPY --from=zoomsdk . ${BOT_DIR}/zoomsdk
COPY --from=meetingbot . /src/meeting-bot
RUN test -f ${BOT_DIR}/zoomsdk/h/zoom_sdk.h \
      || { echo "BARVIS_SOUND_ZOOM_SDK_DIR is not the unpacked Zoom Meeting SDK (no h/zoom_sdk.h)"; exit 1; } \
    && rm -rf /src/meeting-bot/build \
    && cmake -S /src/meeting-bot -B ${BOT_DIR}/build -DZOOM_SDK_DIR=${BOT_DIR}/zoomsdk \
       -DCMAKE_BUILD_TYPE=Release \
    && cmake --build ${BOT_DIR}/build -j"$(nproc)" \
    && test -x ${BOT_DIR}/build/athena-meeting-bot

# sound-api with the bot. The SDK ends up inside this image: it stays on this machine,
# never push it to a registry (compose.meeting.yml names it odysseus-sound-meeting).
FROM server AS meeting
ARG SDK_LIBS
ARG BOT_DIR
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libcurl4t64 libopus0 libopusenc0 libogg0 $SDK_LIBS \
    && rm -rf /var/lib/apt/lists/*
COPY --from=zoomsdk . ${BOT_DIR}/zoomsdk
COPY --from=meeting-bot-build ${BOT_DIR}/build/athena-meeting-bot ${BOT_DIR}/athena-meeting-bot
COPY docker/athena-meeting-bot-run ${BOT_DIR}/athena-meeting-bot-run
# The SDK's soname is libmeetingsdk.so.1 but it ships as libmeetingsdk.so (the spike's
# link trick), made in the image's copy, never in the owner's folder. Then fail the build
# if the bot misses a library, seen as the launcher starts it: an environment with only
# the variables meetings.py keeps (_KEEP_ENV), through the start script.
RUN sdk=${BOT_DIR}/zoomsdk \
    && if [ ! -e $sdk/libmeetingsdk.so.1 ]; then ln -s libmeetingsdk.so $sdk/libmeetingsdk.so.1; fi \
    && chmod 0755 ${BOT_DIR}/athena-meeting-bot ${BOT_DIR}/athena-meeting-bot-run \
    && missing="$(env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp \
         ${BOT_DIR}/athena-meeting-bot-run --ldd | grep 'not found' || true)" \
    && if [ -n "$missing" ]; then echo "missing libraries:"; echo "$missing"; exit 1; fi
ENV CONVO_MEETING_BOT_PATH=${BOT_DIR}/athena-meeting-bot-run

# The default target: the server image without the bot (see the top).
FROM server
