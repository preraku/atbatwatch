FROM golang:1.26-alpine AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/delivery/ ./cmd/delivery/
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o atbatwatch ./cmd/delivery/

FROM alpine:3.21
RUN apk add --no-cache ca-certificates tzdata
COPY --from=builder /build/atbatwatch /usr/local/bin/atbatwatch
ENTRYPOINT ["atbatwatch"]
CMD ["--help"]
