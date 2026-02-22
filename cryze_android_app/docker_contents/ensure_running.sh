#!/system/bin/sh

function logwrapper() {
    local message="$@"
    /system/bin/busybox logger -t LogcatWatchdog -p 1 "$message"
}

logwrapper "Starting ensure_running"

if [ "`getprop breakloop`" -eq 1 ]; then
    logwrapper 'breakloop property set, ensure_running exiting pre-install';
    exit 0;
fi

#=============================================
# Fix Android policy routing for macvlan LAN
# Android's netd adds "32000: from all unreachable" which blocks
# incoming connections. We fix it here and run a background watchdog
# because netd re-adds the rule periodically.
#=============================================
fix_network() {
    ip rule del from all unreachable 2>/dev/null
    ip rule add from all lookup main prio 31000 2>/dev/null
    ip route replace default via 10.10.0.1 dev eth0 2>/dev/null
    iptables -F 2>/dev/null
    iptables -P INPUT ACCEPT 2>/dev/null
    iptables -P OUTPUT ACCEPT 2>/dev/null
    iptables -P FORWARD ACCEPT 2>/dev/null
}

logwrapper "Applying initial network fix"
fix_network
logwrapper "Network fix applied"

# Background watchdog: re-apply every 10s if netd reverts
(
    while true; do
        sleep 10
        if ip rule list 2>/dev/null | grep -q unreachable; then
            fix_network
            logwrapper "Network fix re-applied (netd reverted)"
        fi
    done
) &
logwrapper "Network watchdog started"

#=============================================
# Install the app FIRST, before any service stripping
#=============================================
if [ -f "/app/app.apk" ]; then
    logwrapper "Installing cryze with full permissions"
    pm install --abi arm64-v8a -g --full /app/app.apk >> /dockerlogs
    logwrapper "App installed"
else
    logwrapper "No app.apk found, skipping install"
fi

#=============================================
# Disable unnecessary Android packages AFTER install
# These are confirmed safe to disable and save ~500MB RAM
#=============================================
logwrapper "Disabling unnecessary Android packages"
DISABLE_PKGS="
com.android.systemui
com.android.launcher3
com.android.settings
com.android.phone
com.android.bluetooth
com.android.inputmethod.latin
com.android.providers.media.module
com.android.gallery3d
com.android.camera2
com.android.deskclock
com.android.printspooler
com.android.providers.calendar
com.android.calendar
com.android.providers.contacts
com.android.contacts
com.android.documentsui
com.android.music
"
for pkg in $DISABLE_PKGS; do
    pm disable-user --user 0 $pkg 2>/dev/null && logwrapper "Disabled $pkg"
done
logwrapper "Package stripping done"

logwrapper "Starting main loop"

while true; do
    if [ "`getprop breakloop`" -eq 1 ]; then
        logwrapper 'breakloop property set, ensure_running exiting loop';
        break;
    fi

    # Re-apply network fix each loop
    fix_network

    logwrapper "Starting cryze"
    am start -n com.github.xerootg.cryze/.MainActivity;

    # wait for cryze to start
    sleep 5
    cryze_pid=$(pidof com.github.xerootg.cryze)
    if [ -z "$cryze_pid" ]; then
        logwrapper "cryze not started, retrying"
        sleep 10
        continue
    fi
    while [ -d "/proc/$cryze_pid" ]; do
        sleep 1
    done

    sleep 30 # let the wyze servers chill. they don't seem to like requests too close together
done;


