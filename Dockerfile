FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    SITEDIFF_HOST=0.0.0.0 \
    SITEDIFF_PORT=3000

# Chromium 负责页面截图；Noto CJK 保证中文站点截图不缺字
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-noto-cjk ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY sitediff.py webapp.py ./
COPY reports/ ./reports/

# 平台约定：容器监听 3000 端口（readiness/liveness 探针）
EXPOSE 3000
CMD ["python3", "webapp.py"]
