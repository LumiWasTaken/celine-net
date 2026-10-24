# celine-net, the shellbox: Celine's own little Linux computer. The agent's shell commands run
# here (the `shellbox` service in docker-compose.yml, docs/DEPLOY.md "Shellbox").
#
# It is the user `celine` with sudo, so apt works. Only its home, /home/celine, is a
# volume it writes; everything else is this image and resets when the container is
# recreated.
# shellboxd.py gives the app its terminals (tmux) and runs as root, so nothing it does
# in here stops it; everything it starts for it (terminals, ~/boot.sh) runs as celine.

# the commit and the time of this build, for the greeting and `cat /etc/shellbox/version`:
# made again whenever shellbox/ changes
FROM alpine:3.24.2 AS version
ARG SHELLBOX_COMMIT=dev
RUN --mount=type=bind,source=shellbox,target=/shellbox \
    printf 'version %s\nbuilt %s\n' "$SHELLBOX_COMMIT" "$(date -u +%Y-%m-%dT%H:%MZ)" > /version

FROM debian:trixie

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        sudo ca-certificates tzdata man-db tmux \
        bash-completion less vim nano tree file htop procps psmisc \
        curl wget git openssh-client rsync sqlite3 \
        zip unzip xz-utils bzip2 \
        iproute2 iputils-ping dnsutils netcat-openbsd \
        build-essential pkg-config \
        python3 python3-pip python3-venv \
        ffmpeg pipx fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Node 22 (REA and the Anatomy skill need it; Debian 13 has 20), npm with it, pinned by
# its sha256 per architecture (nodejs.org's SHASUMS256.txt for the version)
ARG NODE_VERSION=22.23.3
ARG NODE_SHA256_X64=df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de
ARG NODE_SHA256_ARM64=a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f
RUN arch=$(dpkg --print-architecture | sed 's/amd64/x64/') \
    && case $arch in x64) sum=$NODE_SHA256_X64 ;; arm64) sum=$NODE_SHA256_ARM64 ;; esac \
    && curl -fsSLo /tmp/node.tar.xz https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz \
    && echo "$sum  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --exclude=CHANGELOG.md \
           --exclude=LICENSE --exclude=README.md \
    && rm /tmp/node.tar.xz && node --version

# REA (github.com/morluto/rea): the `rea` CLI and its MCP server (`rea mcp`, in the
# opencode config shellbox/agents/opencode.py writes), pinned. It needs procps' ps (above).
# The native engines (Ghidra, JADX, a JDK) it installs itself when a target needs one.
ARG REA_VERSION=6.7.0
RUN npm install -g --no-fund --no-audit rea-agents@${REA_VERSION} \
    && npm cache clean --force && rea --help > /dev/null

# opencode: the release binary, pinned by its sha256 (the app's Dockerfile pins bw the
# same way). `agent opencode` runs it on a task; `opencode serve` is its coding agent's
# server (shellbox/agents/opencode.py, the `code` skill), started by shellboxd as it.
ARG OPENCODE_VERSION=1.19.3
ARG OPENCODE_SHA256=13a8fb2978302071b86f9d7e00da9c8895f9cd846978379c3bdc6a5333536c9b
RUN python3 -c "import hashlib, io, os, tarfile, urllib.request; \
v = os.environ['OPENCODE_VERSION']; \
data = urllib.request.urlopen(f'https://github.com/anomalyco/opencode/releases/download/v{v}/opencode-linux-x64.tar.gz').read(); \
assert hashlib.sha256(data).hexdigest() == os.environ['OPENCODE_SHA256'], 'opencode: the sha256 does not match'; \
tarfile.open(fileobj=io.BytesIO(data)).extract('opencode', '/usr/local/bin')" \
    && chmod 755 /usr/local/bin/opencode && opencode --version

# The Anatomy skill (github.com/wheresryan22/anatomy): isometric SVG figures, built with
# Node and checked in Chromium. Pinned to one commit; it reads /opt/skills/anatomy/SKILL.md.
ARG ANATOMY_COMMIT=1482e8469b0a9e0d8cc18ea2740a01423d29ae80
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        chromium \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/skills \
    && git clone -q https://github.com/wheresryan22/anatomy.git /opt/skills/anatomy \
    && git -C /opt/skills/anatomy checkout -q ${ANATOMY_COMMIT} \
    && rm -rf /opt/skills/anatomy/.git \
    && chmod -R a+rX /opt/skills
ENV CHROME_PATH=/usr/bin/chromium PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# its uid and gid, its home's owner: docker-compose.yml names it once (x-her-uid) for
# this image, its browser's and the app, which gives its home to it
ARG HER_UID=1000
RUN groupadd --gid "$HER_UID" celine \
    && useradd --uid "$HER_UID" --gid celine --create-home --shell /bin/bash celine \
    && echo 'celine ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/celine \
    && chmod 440 /etc/sudoers.d/celine \
    && printf 'Defaults !use_pty\n' > /etc/sudoers.d/shellbox \
    && chmod 440 /etc/sudoers.d/shellbox
# !use_pty: sudo runs a command in its terminal itself, not in a pty of its own, so the
# terminal shows what runs (sudo apt-get), and `sudo -i` is a shell in front like any other
RUN --mount=type=bind,source=shellbox/etc/bash.bashrc.add,target=/tmp/bash.bashrc.add \
    cat /tmp/bash.bashrc.add >> /etc/bash.bashrc
WORKDIR /home/celine

# The agent's files, each COPY --link and nothing after them but settings (no RUN, no WORKDIR): a
# change to one rebuilds only its layer, and a builder with only a registry cache (CI)
# needs none of the big layers above.
COPY --link shellbox/etc/tmux.conf /etc/shellbox/tmux.conf
COPY --link --chmod=444 shellbox/etc/README.md /etc/shellbox/README.md
COPY --link shellbox/etc/profile.sh /etc/profile.d/shellbox.sh
COPY --link shellbox/etc/apt-lock.conf /etc/apt/apt.conf.d/90shellbox
COPY --link --chmod=755 shellbox/etc/reboot /usr/local/sbin/reboot
# curl, wget and git tag every file they bring from the net (user.celine.origin, net-tag),
# so shellboxd can tell its own files from the net's; /usr/local/bin comes first, also
# under sudo. After every RUN that downloads, so the build uses the plain tools.
COPY --link --chmod=755 shellbox/net-tag.py /usr/local/lib/shellbox/net-tag
COPY --link --chmod=755 shellbox/etc/net-wrap /usr/local/bin/curl
COPY --link --chmod=755 shellbox/etc/net-wrap /usr/local/bin/wget
COPY --link --chmod=755 shellbox/etc/net-wrap /usr/local/bin/git
COPY --link --chmod=755 shellbox/agent.py /usr/local/bin/agent
COPY --link shellbox/agents/ /usr/local/lib/shellbox/agents/
COPY --link shellbox/shellboxd.py /usr/local/bin/shellboxd
COPY --link --from=version /version /etc/shellbox/version
EXPOSE 8770

CMD ["python3", "/usr/local/bin/shellboxd"]
