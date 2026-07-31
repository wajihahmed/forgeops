FROM alpine:3.19

RUN apk add --no-cache git jq

COPY clone-and-copy.sh /usr/local/bin/clone-and-copy
RUN chmod +x /usr/local/bin/clone-and-copy

ENTRYPOINT ["clone-and-copy"]
