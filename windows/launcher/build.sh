#!/bin/sh
# Published binaries are built with go1.26.11. Set GO to the absolute path of your Go compiler.
set -eu
cd "$(dirname "$0")"
export GOOS=windows CGO_ENABLED=0 GOAMD64=v1 GOARM64=v8.0
export GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOFLAGS= GOEXPERIMENT= GOWORK=off
for arch in amd64 arm64; do
    name="$arch"
    if [ "$arch" = amd64 ]; then name=x64; fi
    GOARCH="$arch" "${GO:-go}" build -mod=readonly -trimpath -buildvcs=false \
        -ldflags="-s -w -H windowsgui -buildid=" -o "../CloudLinuxWorkspace-$name.exe" .
done
