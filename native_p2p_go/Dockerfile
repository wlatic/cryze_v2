FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod ./
RUN go mod download 2>/dev/null || true
COPY . .
RUN CGO_ENABLED=0 go build -o /gwell-proxy ./cmd/gwell-proxy/

FROM alpine:3.19
RUN apk add --no-cache ca-certificates
COPY --from=builder /gwell-proxy /gwell-proxy
ENTRYPOINT ["/gwell-proxy"]
