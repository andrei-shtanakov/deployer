FROM python:3.12-slim AS a
RUN true
FROM python:3.12-slim extra
COPY --from=a /etc/hostname /hostname-a
