ARG ASTRBOT_IMAGE=soulter/astrbot:latest
FROM ${ASTRBOT_IMAGE}

COPY requirements.txt /tmp/astrbot-plugin-maib50-requirements.txt
RUN python -m pip install --no-cache-dir \
    -r /tmp/astrbot-plugin-maib50-requirements.txt \
    && rm /tmp/astrbot-plugin-maib50-requirements.txt

WORKDIR /AstrBot
