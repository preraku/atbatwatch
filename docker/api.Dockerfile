FROM golang:1.26-alpine AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/api/ ./cmd/api/
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o atbatwatch ./cmd/api/

FROM alpine:3.21
RUN apk add --no-cache ca-certificates tzdata wget
COPY --from=builder /build/atbatwatch /usr/local/bin/atbatwatch
ENTRYPOINT ["atbatwatch"]
CMD ["--help"]
