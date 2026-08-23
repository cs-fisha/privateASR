FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} HTTPS_PROXY=${HTTPS_PROXY} NO_PROXY=${NO_PROXY}
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip libsndfile1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server/requirements-common.txt server/requirements-gpu.txt /app/
RUN pip3 install --upgrade pip && pip3 install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124 && pip3 install -r requirements-gpu.txt
RUN pip3 install torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
COPY server/app /app/app
EXPOSE 8080
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
