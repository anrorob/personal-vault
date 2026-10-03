FROM python:3.13-slim
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Exact official binary, verified before extraction. Weights are a read-only external mount.
ADD https://github.com/ggml-org/llama.cpp/releases/download/b10818/llama-b10818-bin-ubuntu-x64.tar.gz /tmp/llama.tar.gz
RUN echo '251f17d584d4074bc2a2aca9a32956e900828931c9df7c67933d3ac5f592abfd  /tmp/llama.tar.gz' | sha256sum -c - \
    && mkdir /opt/llama && tar -xzf /tmp/llama.tar.gz -C /opt/llama --strip-components=1 \
    && rm /tmp/llama.tar.gz
COPY video_analyser/server.py /app/server.py
COPY video_analyser/ken_infer.py /app/ken_infer.py
COPY backend/app/ken_config.py backend/app/ken_style.py backend/app/ken_repetition.py /app/
COPY video_analyser/native_input.py /app/native_input.py
COPY backend/app/ken_attestation.py /app/ken_attestation.py
COPY video_analyser/service_input.py /app/service_input.py
COPY video_analyser/video_ffmpeg.py /app/video-bin/ffmpeg
RUN sed -i 's/\r$//' /app/video-bin/ffmpeg && chmod 755 /app/video-bin/ffmpeg \
    && ln -s /usr/bin/ffprobe /app/video-bin/ffprobe
RUN mkdir /scratch && chown 65534:65534 /scratch
USER 65534:65534
CMD ["python", "/app/server.py"]
