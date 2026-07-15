FROM golang:1.25-alpine AS build

RUN apk add --no-cache ca-certificates
WORKDIR /src
COPY deploy/icloud-privacy-mail/source/ .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/icloud-privacy-mail ./cmd/panel

FROM alpine:3.22

RUN apk add --no-cache ca-certificates tzdata
COPY --from=build /out/icloud-privacy-mail /usr/local/bin/icloud-privacy-mail

VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/icloud-privacy-mail"]
CMD ["--host", "0.0.0.0", "--port", "8787", "--config", "", "--data", "/data/state.json"]
