FROM python:3.13-slim-trixie

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev swig make git ca-certificates && \
    git clone --depth 1 https://github.com/joan2937/lg /tmp/lg && \
    make -C /tmp/lg && make -C /tmp/lg install && ldconfig && \
    rm -rf /tmp/lg && \
    pip install --no-cache-dir \
      "Flask==3.1.1" \
      "gpiozero==2.0.1" \
      "lgpio==0.2.2.0" && \
    apt-get purge -y --auto-remove gcc python3-dev swig make git && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /app/static && \
    python -c "import urllib.request; urllib.request.urlretrieve('https://cdn.jsdelivr.net/gh/davidshimjs/qrcodejs@gh-pages/qrcode.min.js', '/app/static/qrcode.min.js')"

COPY app.py ./

ENV HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data \
    GPIOZERO_PIN_FACTORY=lgpio

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/api/health').read()" || exit 1

CMD ["python", "app.py"]
