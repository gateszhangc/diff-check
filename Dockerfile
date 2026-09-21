FROM nginx:alpine

COPY reports/ /usr/share/nginx/html/

EXPOSE 80
