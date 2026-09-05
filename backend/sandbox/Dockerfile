FROM wcai-sandbox:latest

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest ruff

ENV HOME=/tmp \
    PYTHONIOENCODING=utf-8 \
    MPLBACKEND=Agg

WORKDIR /workspace
USER 65534:65534

CMD ["/bin/bash"]
