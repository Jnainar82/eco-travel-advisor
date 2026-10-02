#!/bin/sh
# Single-container launcher: actions server + Rasa (internal only) + nginx on :7860
rasa run actions --port 5055 &
rasa run --port 5005 --interface 127.0.0.1 --credentials credentials.yml &
exec nginx -c /app/nginx.conf -g 'daemon off;'
