# Pin the base distribution and FFmpeg major version. FFmpeg 8 has shown an
# end-of-clip concat stall in this workload; retest that reproduction before
# upgrading to Alpine 3.23+ or FFmpeg 8.
FROM python:3.14-alpine3.22

RUN apk add --no-cache \
    'ffmpeg<7' \
    git \
    && python -m pip install --no-cache-dir --upgrade pip

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY blinkbridge /app/blinkbridge

WORKDIR /app/

ENTRYPOINT ["python", "-m", "blinkbridge.main"]
