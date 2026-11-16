# XFCE desktop for the Scraperack workspaces plugin: KasmVNC (its Xvnc serves the web
# client and the WebSocket on 6080), Firefox, and a "user" account (uid 1000) with
# passwordless sudo. The plugin's executor starts the display and the session; the image's
# command only exits 0 because Docker needs one to export the image.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

# KasmVNC is GPL-2.0 (its web client MPL-2.0), from its GitHub release and pinned by checksum.
# Source: https://github.com/kasmtech/KasmVNC/tree/v1.5.2
ARG KASMVNC_VERSION=1.5.2
ARG KASMVNC_SHA256=5f4bfda43e89178782eac37ae463b1d95d16ac5e45da8b7989ec75404e764993

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSLo /tmp/kasmvnc.deb "https://github.com/kasmtech/KasmVNC/releases/download/v${KASMVNC_VERSION}/kasmvncserver_debian_bookworm_${KASMVNC_VERSION}_amd64.deb" \
    && echo "${KASMVNC_SHA256}  /tmp/kasmvnc.deb" | sha256sum -c - \
    && apt-get install -y --no-install-recommends \
        /tmp/kasmvnc.deb \
        adwaita-icon-theme \
        dbus \
        dbus-x11 \
        e2fsprogs \
        firefox-esr \
        fonts-dejavu-core \
        less \
        mousepad \
        nano \
        procps \
        python3 \
        ristretto \
        sudo \
        thunar \
        tzdata \
        x11-xserver-utils \
        xfce4-panel \
        xfce4-session \
        xfce4-settings \
        xfce4-terminal \
        xfconf \
        xfdesktop4 \
        xfwm4 \
    && apt-get clean \
    && rm -rf /tmp/kasmvnc.deb /var/lib/apt/lists/* /usr/share/man/* \
    && find /usr/share/doc -mindepth 1 -maxdepth 1 ! -name kasmvncserver -exec rm -rf {} +

# The root filesystem is read-only in the microVM: the machine ID lives in /run, and the
# workspace volume is mounted at /mnt/workspace.
RUN useradd --create-home --uid 1000 --shell /bin/bash --groups sudo,audio,video user \
    && echo "user ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/user \
    && chmod 0440 /etc/sudoers.d/user \
    && ln -sf /run/machine-id /etc/machine-id \
    && mkdir -p /var/lib/dbus /mnt/workspace \
    && ln -sf /etc/machine-id /var/lib/dbus/machine-id

# No compositor: the VM renders in software, and compositing makes every redraw slower.
RUN for file in /usr/share/xfwm4/defaults /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml; do         [ -f "$file" ] && sed -i -e 's/use_compositing=true/use_compositing=false/'             -e 's/name="use_compositing" type="bool" value="true"/name="use_compositing" type="bool" value="false"/'             "$file";     done; grep -rh use_compositing /usr/share/xfwm4/defaults /etc/xdg/xfce4 2>/dev/null || true

CMD ["/bin/true"]
