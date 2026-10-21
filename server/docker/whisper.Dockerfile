# whisper.cpp `whisper-server` for odysseus-sound (compose.yml services whisper-gpu and
# whisper-cpu). Built from a pinned whisper.cpp release, as one static binary.
#
#   BACKEND       cuda (NVIDIA GPU; the host needs the NVIDIA Container Toolkit) or cpu
#   CUDA_ARCH     GPU compute capability without the dot, ";"-separated (RTX 3060: 86).
#                 Fewer architectures build faster; a missing one fails at run time with
#                 "no kernel image is available".
#   CUDA_VERSION  must not be newer than the host driver supports ("CUDA Version" in
#                 nvidia-smi). 12.4 runs on any driver for CUDA 12.x (minor version
#                 compatibility).
#   NATIVE        ON tunes the CPU code for the machine that builds the image (the server
#                 builds its own image, so that is the machine it runs on).
ARG BACKEND=cpu
ARG CUDA_VERSION=12.4.1
ARG UBUNTU_VERSION=22.04

FROM ubuntu:${UBUNTU_VERSION} AS build-cpu
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} AS build-cuda

FROM build-${BACKEND} AS build
ARG BACKEND
ARG WHISPER_REF=v1.9.5
ARG CUDA_ARCH=75;80;86;89
ARG NATIVE=ON
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "$WHISPER_REF" https://github.com/ggml-org/whisper.cpp /src
# The build machine has no GPU driver: link against the CUDA stub libcuda.
RUN if [ "$BACKEND" = cuda ]; then \
        flags="-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=$CUDA_ARCH"; \
        ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1; \
        export LIBRARY_PATH=/usr/local/cuda/lib64/stubs LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs; \
    else flags=""; fi \
    && cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE="$NATIVE" -DWHISPER_BUILD_TESTS=OFF -DWHISPER_SDL2=OFF $flags \
    && cmake --build /build -j"$(nproc)" --target whisper-server

FROM ubuntu:${UBUNTU_VERSION} AS run-cpu
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION} AS run-cuda

FROM run-${BACKEND}
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/bin/whisper-server /usr/local/bin/whisper-server
# Model file (WHISPER_MODEL, in the /models volume) and extra flags (WHISPER_ARGS) come
# from compose.yml.
ENV WHISPER_MODEL=ggml-large-v3-turbo-q5_0.bin WHISPER_ARGS=""
EXPOSE 8178
ENTRYPOINT ["sh", "-c", "exec whisper-server -m \"/models/$WHISPER_MODEL\" --host 0.0.0.0 --port 8178 $WHISPER_ARGS"]
