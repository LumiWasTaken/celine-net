# wa-link: the WhatsApp connector (one linked device). Build from the repository root:
#   docker build -f wa-link/Dockerfile .
#
# The device store and the journal live on the volume at /data, never in the image.
# Only one process may use a device store; wa-link locks its state folder.

FROM golang:1.27.1-alpine3.24 AS build
WORKDIR /src
COPY wa-link/go.mod wa-link/go.sum ./
RUN go mod download
COPY wa-link/*.go ./
# the compiler's cache stays in the builder, so a change compiles only what it touches
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/wa-link .

FROM alpine:3.24.2
RUN apk add --no-cache tzdata && adduser -D -u 1000 celine && mkdir /data && chown celine:celine /data
COPY --link --from=build /out/wa-link /usr/local/bin/wa-link
USER celine
VOLUME ["/data"]
EXPOSE 8767

# healthy = the doors answer a known key (the first client in /data/connector-clients)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["sh", "-c", "k=$(head -n1 /data/connector-clients); k=${k%%,*}; k=${k#*:}; k=${k%:*}; wget -q -O /dev/null --header \"Authorization: Bearer $k\" http://127.0.0.1:8767/admin/status"]

CMD ["wa-link", "-store", "/data/store.db", "-state", "/data/state", "-listen", "0.0.0.0:8767"]
