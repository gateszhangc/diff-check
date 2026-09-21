FROM nginx:alpine

COPY reports/ /usr/share/nginx/html/

# 平台约定：容器监听 3000 端口（readiness/liveness 探针）
RUN printf 'server {\n    listen 3000;\n    server_name _;\n    root /usr/share/nginx/html;\n    index index.html;\n}\n' > /etc/nginx/conf.d/default.conf

EXPOSE 3000
