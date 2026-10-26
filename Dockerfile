# syntax=docker/dockerfile:1
# AppleMusicDecrypt comes from the amd/ git submodule (liamniou/AppleMusicDecrypt, branch v2)

FROM python:3.15-slim-bookworm AS tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config git zlib1g-dev cmake ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth=1 https://github.com/gpac/gpac.git /src/gpac \
 && cd /src/gpac && ./configure --static-bin && make -j"$(nproc)" && make install DESTDIR=/out
RUN git clone --depth=1 https://github.com/axiomatic-systems/Bento4.git /src/Bento4 \
 && mkdir /src/Bento4/cmakebuild && cd /src/Bento4/cmakebuild \
 && cmake -DCMAKE_BUILD_TYPE=Release .. && make -j"$(nproc)" mp4edit mp4extract mp4decrypt \
 && install -m 0755 mp4edit mp4extract mp4decrypt /out/usr/local/bin/

FROM python:3.15-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 AMD_DIR=/amd DATA_DIR=/data HOME=/tmp
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=tools /out/usr/local/ /usr/local/
RUN ln -sf /usr/local/bin/MP4Box /usr/local/bin/mp4box && ldconfig

WORKDIR /amd
COPY amd/pyproject.toml amd/poetry.lock ./
RUN pip install poetry \
 && poetry config virtualenvs.create false \
 && poetry install --only main --no-root --no-interaction \
 && pip uninstall -y poetry
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY amd/main.py ./
COPY amd/assets ./assets
COPY amd/src ./src
COPY app /app

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
WORKDIR /app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
