FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir "Flask==3.1.1" "requests==2.32.3" "PyYAML==6.0.2"

COPY app.py ./

ENV HOST=0.0.0.0 \
    PORT=8080 \
    RPIS_FILE=/config/rpis.yml

EXPOSE 8080
VOLUME ["/config"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/api/health').read()" || exit 1

CMD ["python", "app.py"]
