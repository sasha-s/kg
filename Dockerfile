# syntax=docker/dockerfile:1.4
# ============================================================================
# kg Dev Container - Builder Stage
# ============================================================================
FROM node:20-bookworm AS builder

# Pin versions for reproducible builds
ARG BTOP_VERSION=1.4.0
ARG LAZYGIT_VERSION=0.44.1
ARG NVIM_VERSION=v0.11.5
ARG TYPST_VERSION=0.13.1
ARG JJ_VERSION=0.36.0

# Install minimal dependencies for downloading and extracting
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y \
    curl \
    wget \
    xz-utils \
    bzip2 \
    gzip \
    tar \
    make

# Detect architecture once for all tools
RUN ARCH=$(uname -m) && \
    echo "Building for architecture: $ARCH" && \
    echo "$ARCH" > /tmp/arch

# Download and install btop
RUN ARCH=$(cat /tmp/arch) && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        BTOP_ARCH="aarch64"; \
    else \
        BTOP_ARCH="x86_64"; \
    fi && \
    curl -L "https://github.com/aristocratos/btop/releases/download/v${BTOP_VERSION}/btop-${BTOP_ARCH}-linux-musl.tbz" \
    | tar -xj -C /tmp && \
    cd /tmp/btop && \
    make install PREFIX=/usr/local && \
    rm -rf /tmp/btop

# Download and install lazygit
RUN ARCH=$(cat /tmp/arch) && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        LAZYGIT_ARCH="Linux_arm64"; \
    else \
        LAZYGIT_ARCH="Linux_x86_64"; \
    fi && \
    curl -L "https://github.com/jesseduffield/lazygit/releases/download/v${LAZYGIT_VERSION}/lazygit_${LAZYGIT_VERSION}_${LAZYGIT_ARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin lazygit && \
    chmod +x /usr/local/bin/lazygit

# Download and install Neovim
RUN ARCH=$(cat /tmp/arch) && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        NVIM_ARCH="linux-arm64"; \
    else \
        NVIM_ARCH="linux-x86_64"; \
    fi && \
    curl -LO "https://github.com/neovim/neovim/releases/download/${NVIM_VERSION}/nvim-${NVIM_ARCH}.tar.gz" && \
    tar -xzf "nvim-${NVIM_ARCH}.tar.gz" -C /usr/local --strip-components=1 && \
    rm "nvim-${NVIM_ARCH}.tar.gz"

# Download and install Typst
RUN ARCH=$(cat /tmp/arch) && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        TYPST_ARCH="aarch64-unknown-linux-musl"; \
    else \
        TYPST_ARCH="x86_64-unknown-linux-musl"; \
    fi && \
    curl -L "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-${TYPST_ARCH}.tar.xz" \
    | tar -xJ -C /tmp && \
    mv /tmp/typst-${TYPST_ARCH}/typst /usr/local/bin/typst && \
    chmod +x /usr/local/bin/typst && \
    rm -rf /tmp/typst-${TYPST_ARCH}

# Download and install jj (Jujutsu VCS)
RUN ARCH=$(cat /tmp/arch) && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
        JJ_ARCH="aarch64-unknown-linux-musl"; \
    else \
        JJ_ARCH="x86_64-unknown-linux-musl"; \
    fi && \
    curl -L "https://github.com/jj-vcs/jj/releases/download/v${JJ_VERSION}/jj-v${JJ_VERSION}-${JJ_ARCH}.tar.gz" \
    | tar -xz -C /tmp && \
    mv /tmp/jj /usr/local/bin/jj && \
    chmod +x /usr/local/bin/jj

# ============================================================================
# Final Stage - kg Development Environment
# ============================================================================
FROM node:20-bookworm

# Install system dependencies (minimal set)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y \
    # Version control & shell
    git \
    git-lfs \
    zsh \
    # System utilities
    sudo \
    curl \
    wget \
    jq \
    nano \
    vim \
    tmux \
    # Networking
    iproute2 \
    # Compression
    xz-utils \
    bzip2 \
    gzip \
    tar \
    # Development tools
    ripgrep \
    sqlite3 \
    lsof \
    htop \
    build-essential \
    fd-find \
    fzf \
    tree \
    stow \
    # PAM headers for vibetunnel
    libpam0g-dev \
    # Python development headers
    python3-dev

# Install GitHub CLI
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    mkdir -p -m 755 /etc/apt/keyrings && \
    wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null && \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh

# Copy binaries from builder stage
COPY --from=builder /usr/local/bin/btop /usr/local/bin/btop
COPY --from=builder /usr/local/bin/lazygit /usr/local/bin/lazygit
COPY --from=builder /usr/local/bin/nvim /usr/local/bin/nvim
COPY --from=builder /usr/local/lib/nvim /usr/local/lib/nvim
COPY --from=builder /usr/local/share/nvim /usr/local/share/nvim
COPY --from=builder /usr/local/bin/typst /usr/local/bin/typst
COPY --from=builder /usr/local/bin/jj /usr/local/bin/jj

# Create symlink for fd
RUN ln -sf /usr/bin/fdfind /usr/local/bin/fd

# Install uv (Python package manager) and Python 3.12
RUN --mount=type=cache,target=/root/.cache/uv \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx && \
    chmod +x /usr/local/bin/uv /usr/local/bin/uvx && \
    uv python install 3.12

# Install Oh My Zsh and configure zsh as default shell
USER node
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended
USER root
RUN chsh -s /bin/zsh node

# Update npm and install global packages
RUN --mount=type=cache,target=/root/.npm \
    npm install -g npm@latest pnpm && \
    npm install -g typescript tsx tree-sitter-cli && \
    npm install -g node-addon-api && \
    NODE_PATH=/usr/local/lib/node_modules npm install -g vibetunnel && \
    # Fix vibetunnel symlink - the npm shim uses require() on a bundled executable which doesn't work
    ln -sf /usr/local/lib/node_modules/vibetunnel/dist/vibetunnel-cli /usr/local/bin/vibetunnel

# Configure sudo for node user (minimal permissions)
RUN echo "node ALL=(ALL) NOPASSWD: /usr/bin/chown *" > /etc/sudoers.d/node && \
    chmod 0440 /etc/sudoers.d/node

# Set up workspace directories and caches
RUN mkdir -p /workspace /commandhistory /npm-cache /uv-cache && \
    chown -R node:node /workspace /commandhistory /npm-cache /uv-cache && \
    chmod 1777 /tmp

# Configure environment variables
ENV EDITOR=vim \
    VISUAL=vim \
    SHELL=/bin/zsh \
    PATH="/home/node/.npm-global/bin:$PATH" \
    CLAUDE_CODE_MAX_OUTPUT_TOKENS=65535 \
    npm_config_cache=/npm-cache \
    UV_CACHE_DIR=/uv-cache \
    VIBETUNNEL_BUNDLED=true

WORKDIR /workspace

# Switch to node user for remaining setup
USER node

# Configure npm for node user
RUN mkdir -p /home/node/.npm-global && \
    npm config set prefix /home/node/.npm-global

# Initialize Git LFS for node user
RUN git lfs install

# Install Rust via rustup (nightly toolchain)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain nightly && \
    . /home/node/.cargo/env && \
    rustup component add rust-src rustfmt clippy

# Install LazyVim configuration
RUN git clone https://github.com/LazyVim/starter /home/node/.config/nvim && \
    rm -rf /home/node/.config/nvim/.git && \
    echo 'vim.keymap.set("i", "jj", "<Esc>", { noremap = true })' >> /home/node/.config/nvim/lua/config/keymaps.lua

# Install Claude Code via official install script
RUN curl -fsSL https://claude.ai/install.sh | bash

# Configure bash
RUN echo 'export HISTFILE=/commandhistory/.bash_history' >> /home/node/.bashrc && \
    echo 'export PROMPT_COMMAND="history -a; $PROMPT_COMMAND"' >> /home/node/.bashrc && \
    echo 'export PATH="/home/node/.cargo/bin:/home/node/.local/bin:/workspace/.venv/bin:/home/node/.npm-global/bin:$PATH"' >> /home/node/.bashrc && \
    echo 'alias cc="claude --dangerously-skip-permissions"' >> /home/node/.bashrc && \
    echo 'alias vts="vibetunnel --port 4020 --no-auth"' >> /home/node/.bashrc && \
    echo 'alias vim="nvim"' >> /home/node/.bashrc && \
    echo 'alias vi="nvim"' >> /home/node/.bashrc

# Configure zsh
RUN echo 'export HISTFILE=/commandhistory/.zsh_history' >> /home/node/.zshrc && \
    echo 'setopt SHARE_HISTORY' >> /home/node/.zshrc && \
    echo 'export PATH="/home/node/.cargo/bin:/home/node/.local/bin:/workspace/.venv/bin:/home/node/.npm-global/bin:$PATH"' >> /home/node/.zshrc && \
    echo 'alias cc="claude --dangerously-skip-permissions"' >> /home/node/.zshrc && \
    echo 'alias vts="vibetunnel --port 4020 --no-auth"' >> /home/node/.zshrc && \
    echo 'alias vim="nvim"' >> /home/node/.zshrc && \
    echo 'alias vi="nvim"' >> /home/node/.zshrc

# Copy entrypoint script
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bin/zsh"]
