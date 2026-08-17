FROM --platform=$BUILDPLATFORM golang:1.24-alpine AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .

ARG VERSION=dev
ARG COMMIT=none
ARG BUILD_DATE=unknown
ARG TARGETOS=linux
ARG TARGETARCH=amd64
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build -trimpath -ldflags="-s -w -X main.version=${VERSION} -X main.commit=${COMMIT} -X main.buildDate=${BUILD_DATE}" -o /out/control-plane ./cmd/control-plane

FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /out/control-plane /control-plane
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/control-plane"]
