FROM golang:1.23.3-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Add project-specific native libraries, tools, and the selected provider CLI here.
# RUN go install example.com/your/tool@v1.2.3
WORKDIR /workspace
