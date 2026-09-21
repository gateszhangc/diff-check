FROM nginx:alpine

COPY reports/ /usr/share/nginx/html/

# 平台约定：容器监听 3000 端口（readiness/liveness 探针）
RUN sed -i 's/listen 80;/listen 3000;/' /etc/nginx/conf.d/default.conf

EXPOSE 3000
