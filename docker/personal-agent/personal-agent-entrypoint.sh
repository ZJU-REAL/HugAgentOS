#!/bin/bash
set -euo pipefail
# The live-screen token is derived from the execd token, so the backend can compute it.
# It is not the execd token: a compromised desktop user cannot use it to run commands.
install -d -m 700 -o 1001 -g 1001 /run/pa-desktop
/opt/python/current/bin/python3 -I -c 'import hashlib,hmac,os;print(hmac.new(os.environ.get("EXECD_ACCESS_TOKEN","").encode(),b"pa-desktop-stream",hashlib.sha256).hexdigest() if os.environ.get("EXECD_ACCESS_TOKEN") else "")' >/run/pa-desktop/stream.token
chown 1001:1001 /run/pa-desktop/stream.token
chmod 600 /run/pa-desktop/stream.token
# execd already holds its private token. Do not pass it to the desktop session.
unset EXECD_ACCESS_TOKEN
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix
chown 1001:1001 /home/pa-desktop
chmod 700 /home/pa-desktop
# A new container owns this private profile. Chrome locks refer to the old hostname.
rm -f /home/pa-desktop/browser/SingletonLock /home/pa-desktop/browser/SingletonCookie /home/pa-desktop/browser/SingletonSocket
# Default browser, terminal and file manager for the desktop user. Rewritten on each start.
install -d -o 1001 -g 1001 /home/pa-desktop/.config /home/pa-desktop/.config/xfce4
install -m 644 -o 1001 -g 1001 /opt/personal-agent/desktop/helpers.rc /home/pa-desktop/.config/xfce4/helpers.rc
install -m 644 -o 1001 -g 1001 /opt/personal-agent/desktop/mimeapps.list /home/pa-desktop/.config/mimeapps.list
mkdir -p /workspace/scratch /workspace/work /workspace/outputs
chown -R 1000:1000 /workspace
# The X cookie stays on the container filesystem. Some hosts (Docker Desktop on macOS)
# do not enforce ownership on bind mounts, so the task user could read it in the home volume.
rm -f /home/pa-desktop/.Xauthority
# Container restarts preserve filesystem sockets and locks, but kill their processes.
# This entrypoint owns display :99; remove only its stale runtime paths before launch.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /run/pa-desktop/vnc.sock
su - pa-desktop -c 'touch /run/pa-desktop/.Xauthority; xauth -f /run/pa-desktop/.Xauthority add :99 . $(mcookie); Xvfb :99 -screen 0 1280x800x24 -auth /run/pa-desktop/.Xauthority -nolisten tcp >/run/pa-desktop/xvfb.log 2>&1 &'
su - pa-desktop -c 'export DISPLAY=:99 XAUTHORITY=/run/pa-desktop/.Xauthority; sleep 1; exec dbus-run-session -- xfce4-session' >/run/pa-desktop/desktop.log 2>&1 &
# Live screen: VNC listens only on a socket in the private directory; pa-stream checks the token.
su - pa-desktop -c 'sleep 2; while true; do x11vnc -display :99 -auth /run/pa-desktop/.Xauthority -rfbport 0 -unixsock /run/pa-desktop/vnc.sock -forever -shared -nopw -quiet -nowf -noscr -ncache 0 -cursor most -xkb -wait 10 -defer 10; sleep 1; done' >/run/pa-desktop/vnc.log 2>&1 &
su - pa-desktop -c 'while true; do /opt/python/current/bin/python3 -I /usr/local/bin/pa-stream; sleep 1; done' >/run/pa-desktop/stream.log 2>&1 &
# PID 1 reaps every orphan and kills what task commands (UID 1000) leave behind;
# the desktop above runs as UID 1001 and is only reaped.
exec sandbox-init 1000
