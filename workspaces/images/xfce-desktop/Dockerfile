# XFCE desktop for the Scraperack workspaces plugin: TigerVNC (Xvnc), noVNC on 6080,
# Firefox, and a "user" account (uid 1000) with passwordless sudo. The plugin's executor
# starts the display and the session; the image's command only exits 0 because Docker
# needs one to export the image.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        adwaita-icon-theme \
        ca-certificates \
        curl \
        dbus \
        dbus-x11 \
        e2fsprogs \
        firefox-esr \
        fonts-dejavu-core \
        less \
        mousepad \
        nano \
        novnc \
        procps \
        python3 \
        python3-websockify \
        ristretto \
        sudo \
        thunar \
        tigervnc-standalone-server \
        tigervnc-tools \
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
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/*

# The root filesystem is read-only in the microVM: the machine ID lives in /run, and the
# workspace volume is mounted at /mnt/workspace.
RUN useradd --create-home --uid 1000 --shell /bin/bash --groups sudo,audio,video user \
    && echo "user ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/user \
    && chmod 0440 /etc/sudoers.d/user \
    && ln -sf /run/machine-id /etc/machine-id \
    && mkdir -p /var/lib/dbus /mnt/workspace \
    && ln -sf /etc/machine-id /var/lib/dbus/machine-id \
    && ln -sf vnc.html /usr/share/novnc/index.html

# No compositor: the VM renders in software, and compositing makes every redraw slower.
RUN for file in /usr/share/xfwm4/defaults /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml; do         [ -f "$file" ] && sed -i -e 's/use_compositing=true/use_compositing=false/'             -e 's/name="use_compositing" type="bool" value="true"/name="use_compositing" type="bool" value="false"/'             "$file";     done; grep -rh use_compositing /usr/share/xfwm4/defaults /etc/xdg/xfce4 2>/dev/null || true

CMD ["/bin/true"]
