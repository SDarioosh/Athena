# odysseus-sound server image: `convo-server serve` and `convo-server mcp --http`.
# Built and run by compose.yml (README "Docker"). Settings come from server/.env at run
# time; nothing secret is baked in.
FROM python:3.12-slim

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
RUN mkdir convo_server && touch convo_server/__init__.py \
    && pip install --no-cache-dir --index-url "$TORCH_INDEX" torch torchaudio torchcodec \
    && pip freeze | grep -E '^(torch|torchaudio|torchcodec)==' > /tmp/torch-pins.txt \
    && pip install --no-cache-dir -c /tmp/torch-pins.txt ".[$EXTRAS]" \
    && pip uninstall -y -q convo-server && rm -rf convo_server build

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
