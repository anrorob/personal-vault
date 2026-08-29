FROM python:3.10-slim

WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y git && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.7.1+cpu torchvision==0.22.1+cpu \
    && pip install --no-cache-dir timm==0.4.12 fairscale==0.4.4 scipy Pillow transformers==4.53.2 \
    fastapi==0.116.1 uvicorn[standard]==0.35.0 \
    "git+https://github.com/xinyu1205/recognize-anything.git@7cb804a8609e9f4b1a50b7f31436d2df40bb9481"
COPY app.py ./app.py
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
