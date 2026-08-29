FROM python:3.10-slim
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir mediapipe==0.10.21 pillow==11.3.0 fastapi==0.116.1 uvicorn[standard]==0.35.0
COPY app.py ./app.py
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
