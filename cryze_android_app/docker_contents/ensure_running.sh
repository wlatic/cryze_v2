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
# Android adds "32000: from all unreachable" which blocks traffic not
# matching fwmark-based rules. Remove it and add fallback to main table.
#=============================================
logwrapper "Applying ip rule fix for macvlan networking"
ip rule del from all unreachable 2>/dev/null
ip rule add from all lookup main prio 31000 2>/dev/null
logwrapper "ip rule fix applied"

#=============================================
# Disable unnecessary Android services/packages
# Saves ~1.5GB RAM in a headless container
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
com.android.dialer
com.android.messaging
com.android.documentsui
com.android.email
com.android.music
"
for pkg in $DISABLE_PKGS; do
    pm disable-user --user 0 $pkg 2>/dev/null && logwrapper "Disabled $pkg"
done

if [ -f "/app/app.apk" ]; then
    logwrapper "Installing cryze with full permissions"
    pm install --abi arm64-v8a -g --full /app/app.apk >> /dockerlogs # log the install to docker logs
    logwrapper "App installed, starting main loop"
else
    logwrapper "No app.apk found, skipping install"
fi


while true; do
    if [ "`getprop breakloop`" -eq 1 ]; then
        logwrapper 'breakloop property set, ensure_running exiting loop';
        break;
    fi

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


