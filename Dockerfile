FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/tinybox .

FROM alpine:3.21
RUN addgroup -S tinybox && adduser -S -G tinybox tinybox
COPY --from=build /out/tinybox /usr/local/bin/tinybox
USER tinybox
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/tinybox"]
