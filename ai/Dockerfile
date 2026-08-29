FROM openvino/ubuntu22_dev@sha256:2fec1614c692c6d5e95d3e6b9fd0903c2fa9cbe128a8b452dffdfa3fed6627ae

USER root
RUN python3 -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu torch==2.7.1 torchvision==0.22.1 && \
    python3 -m pip install --no-cache-dir \
    fastapi==0.116.1 uvicorn==0.35.0 pillow==11.3.0 \
    transformers==4.53.2 einops==0.8.1 timm==1.0.17

WORKDIR /app
COPY download_helper.py /tmp/download_helper.py
RUN python3 /tmp/download_helper.py && rm /tmp/download_helper.py
COPY app.py /app/app.py

RUN useradd --create-home --uid 10001 florence && chown -R florence:florence /app
USER florence
EXPOSE 8080
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
