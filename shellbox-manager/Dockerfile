# shellbox-manager: the shellbox's morning reset, inside the stack (manager.sh)
FROM docker:29.8.2-cli
RUN apk add --no-cache tzdata
COPY --link --chmod=755 shellbox-manager/manager.sh /usr/local/bin/shellbox-manager
ENTRYPOINT ["shellbox-manager"]
