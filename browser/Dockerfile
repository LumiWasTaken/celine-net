# celine-net, the browser box: Celine's own Brave on a screen of its own (the `browser`
# service in docker-compose.yml, docs/DEPLOY.md "Browser").
#
# Everything runs as celine (HER_UID, as in its shellbox), with its home mounted, so its
# profile (~/.browser) and downloads (~/Downloads) are its own. run.sh keeps browserd.py
# running; browserd runs Xvnc (the screen) and Brave (CDP on 127.0.0.1 only) and gives
# the app a small API. Its sandbox needs seccomp:unconfined (compose). Brave, not Chromium:
# Chromium no longer runs Manifest V2 extensions, and the full uBlock Origin is one.

FROM debian:trixie

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg \
        https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg \
    && curl -fsSLo /etc/apt/sources.list.d/brave-browser-release.sources \
        https://brave-browser-apt-release.s3.brave.com/brave-browser.sources \
    && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        brave-browser fonts-liberation fonts-noto-color-emoji tigervnc-standalone-server \
        python3 python3-websockets python3-pil procps tzdata \
    && rm -rf /var/lib/apt/lists/*

# its uid and gid, its home's owner: docker-compose.yml names it once (x-her-uid)
ARG HER_UID=1000
RUN groupadd --gid "$HER_UID" celine \
    && useradd --uid "$HER_UID" --gid celine --create-home --shell /bin/bash celine
USER celine
WORKDIR /home/celine
# COPY --link and nothing after it but settings: a change rebuilds only its own layer, and
# a builder with only a registry cache (CI) needs none of the layers above
COPY --link --chmod=755 browser/run.sh /usr/local/bin/run.sh
COPY --link browser/browserd.py /usr/local/bin/browserd
COPY --link browser/snapshot.py /usr/local/bin/snapshot.py
EXPOSE 8771

CMD ["/usr/local/bin/run.sh"]
