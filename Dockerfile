FROM python:3.12-slim

# pip 源，可覆盖：--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8

RUN pip install --no-cache-dir -i $PIP_INDEX_URL pillow==11.1.0 \
 && rm -rf /root/.cache/pip

WORKDIR /app
COPY dash.py /app/dash.py
RUN mkdir -p /app/logs /app/data /app/fonts

CMD ["python3", "-u", "/app/dash.py"]
