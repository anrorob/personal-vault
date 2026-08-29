FROM python:3.10-slim
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y git g++ libglib2.0-0 libgl1 libxcb1 && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.7.1+cpu torchvision==0.22.1+cpu \
    && pip install --no-cache-dir deepface==0.0.95 tf-keras==2.21.0 opencv-python==4.10.0.84 fastapi==0.116.1 uvicorn[standard]==0.35.0 psutil==7.0.0 \
    && pip install --no-cache-dir --no-build-isolation --no-deps yolox==0.3.0 loguru thop pycocotools tabulate
COPY app.py ./app.py
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
