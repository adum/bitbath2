# The benchmark image: the model harnesses, the toolchains a bot may use, and
# the starter kit, built, at /workspace. `python benchmark.py build` makes the
# build context and runs this; it isn't meant to be built by hand.
#
# One image serves both phases. A model builds its bot in a container of it,
# and the tournament plays the bots in another, so every bot meets the same
# engine and toolchains it was built against.
FROM node:24-bookworm-slim

ARG CODEX_VERSION=0.162.1
ARG CLAUDE_VERSION=2.1.325
ARG OPENCODE_VERSION=1.15.10
ARG RUST_VERSION=1.99.1

# bubblewrap and socat are for Claude's own subprocess sandbox.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python-is-python3 g++ make git ca-certificates curl bubblewrap socat \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@${CODEX_VERSION} \
       @anthropic-ai/claude-code@${CLAUDE_VERSION} opencode-ai@${OPENCODE_VERSION} \
    && npm cache clean --force

ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain ${RUST_VERSION} --no-modify-path

# The Antigravity CLI has no npm package: its release is one binary in an
# archive, pinned here by its address and checksum.
ARG AGY_URL=https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.13-5999302598852608/linux-x64/cli_linux_x64.tar.gz
ARG AGY_SHA512=014b37387703acab1f47cd532c8bb60167a6a3537b0f9891de03f1b1ffa9614307bc83c4b2f7551273b0877d722716b0cd9e26782f6b2a50705db4f05e18c68f
RUN curl -fsSL -o /tmp/agy.tar.gz "${AGY_URL}" \
    && echo "${AGY_SHA512}  /tmp/agy.tar.gz" | sha512sum -c - \
    && mkdir /tmp/agy && tar -xzf /tmp/agy.tar.gz -C /tmp/agy \
    && install -m 0755 "$(find /tmp/agy -type f -name antigravity | head -1)" /usr/local/bin/agy \
    && rm -rf /tmp/agy /tmp/agy.tar.gz

COPY bench/proxy.py /opt/bench/proxy.py
COPY bench/bwrap.py /opt/bench/claude-bwrap
COPY bench/agy_policy.py /opt/bench/agy-policy
COPY kit.json /opt/bench/kit.json
RUN chmod 0555 /opt/bench/claude-bwrap /opt/bench/agy-policy \
    && mkdir -p /home/bench /etc/claude-code \
    && printf '%s\n' '{"sandbox":{"bwrapPath":"/opt/bench/claude-bwrap"}}' \
       > /etc/claude-code/managed-settings.json

# The kit, with the runner built. A container's /workspace is a new volume,
# which Docker fills from this directory, owner included. Building here also
# leaves the crates the kit needs in /opt/cargo, for building without a
# network later. A sample bot in Rust is built too, beside the runner:
# bots/forager_bot.rs as target/release/forager_bot.
COPY --chown=1000:1000 kit /workspace
RUN cd /workspace && cargo build --release && cargo test --release --no-run \
    && for bot in bots/*_bot.rs; do \
         if [ -f "$bot" ]; then rustc -O "$bot" -o "target/release/$(basename "$bot" .rs)" || exit 1; fi; \
       done \
    && chown -R 1000:1000 /workspace && chmod -R a+rX /opt/cargo /opt/rustup

ENV HOME=/home/bench CARGO_NET_OFFLINE=true PYTHONDONTWRITEBYTECODE=1 \
    NODE_USE_ENV_PROXY=1 NO_COLOR=1
WORKDIR /workspace
CMD ["sleep", "infinity"]
