#!/bin/bash
#=============================================
# Cryze Stream Watchdog
# Monitors camera streams and restarts the Docker stack
# when they fail, with smart pre-restart checks.
#=============================================

set -euo pipefail

# --- Configuration (overridable via /root/cryze_v2-main/watchdog.env) ---
COMPOSE_DIR="${COMPOSE_DIR:-/root/cryze_v2-main}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.macvlan-only.yml}"
CONTAINER_APP="${CONTAINER_APP:-cryze_v2-main-cryze_android_app-1}"
CONTAINER_MTX="${CONTAINER_MTX:-cryze_v2-main-mediamtx-1}"
MEDIAMTX_API="${MEDIAMTX_API:-http://127.0.0.1:9997/v3/paths/list}"
EXPECTED_STREAMS="${EXPECTED_STREAMS:-2}"
LOG_FILE="${LOG_FILE:-/var/log/cryze-watchdog.log}"
STATE_FILE="${STATE_FILE:-/tmp/cryze-watchdog-state}"

# Timing (seconds)
BOOT_GRACE="${BOOT_GRACE:-180}"
CHECK_INTERVAL="${CHECK_INTERVAL:-60}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"
OUTAGE_RECHECK="${OUTAGE_RECHECK:-120}"
BASE_COOLDOWN="${BASE_COOLDOWN:-300}"
MAX_COOLDOWN="${MAX_COOLDOWN:-3600}"

# External check targets
PING_TARGETS="${PING_TARGETS:-8.8.8.8 1.1.1.1}"
WYZE_HOST="${WYZE_HOST:-wyze-mars-asrv.wyzecam.com}"

# --- State ---
consecutive_failures=0
restart_count=0
last_restart_time=0

log() {
    local msg="[cryze-watchdog] $*"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $msg" >> "$LOG_FILE"
    logger -t cryze-watchdog "$*"
}

write_state() {
    cat > "$STATE_FILE" <<EOF
state=$1
consecutive_failures=$consecutive_failures
restart_count=$restart_count
last_restart=$(date -d @${last_restart_time} '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo 'never')
timestamp=$(date '+%Y-%m-%d %H:%M:%S')
EOF
}

# --- Health Checks ---

check_internet() {
    for target in $PING_TARGETS; do
        if ping -c 1 -W 3 "$target" &>/dev/null; then
            return 0
        fi
    done
    log "WARN: Internet unreachable (pinged: $PING_TARGETS)"
    return 1
}

check_dns() {
    if host "$WYZE_HOST" &>/dev/null 2>&1 || nslookup "$WYZE_HOST" &>/dev/null 2>&1; then
        return 0
    fi
    log "WARN: DNS resolution failed for $WYZE_HOST"
    return 1
}

check_wyze_api() {
    # Just check that the Wyze server is reachable (TCP connect)
    if curl -s --connect-timeout 5 --max-time 10 "https://$WYZE_HOST" -o /dev/null 2>/dev/null; then
        return 0
    fi
    log "WARN: Wyze server unreachable ($WYZE_HOST)"
    return 1
}

check_container_running() {
    if docker inspect -f '{{.State.Running}}' "$CONTAINER_APP" 2>/dev/null | grep -q true; then
        return 0
    fi
    log "WARN: Container $CONTAINER_APP is not running"
    return 1
}

check_app_process() {
    local pid
    pid=$(docker exec "$CONTAINER_APP" pidof com.github.xerootg.cryze 2>/dev/null || true)
    if [ -n "$pid" ]; then
        return 0
    fi
    log "WARN: Cryze app process not found inside container"
    return 1
}

check_streams() {
    # Try MediaMTX API first (if enabled)
    local api_response
    api_response=$(docker exec "$CONTAINER_MTX" wget -q -O - "$MEDIAMTX_API" 2>/dev/null || true)

    if [ -n "$api_response" ]; then
        # API is available — count paths with active readers/source
        local active
        active=$(echo "$api_response" | grep -c '"ready":true' 2>/dev/null || echo "0")
        if [ "$active" -ge "$EXPECTED_STREAMS" ]; then
            return 0
        fi
        log "WARN: Only $active/$EXPECTED_STREAMS streams ready (via API)"
        return 1
    fi

    # Fallback: check recent MediaMTX logs for publishing activity
    local recent_publishers
    recent_publishers=$(docker logs --since 90s "$CONTAINER_MTX" 2>&1 | grep -c "is publishing" || echo "0")
    if [ "$recent_publishers" -ge "$EXPECTED_STREAMS" ]; then
        return 0
    fi

    # Also check if streams were established earlier and are still alive
    # (no "closed" or "destroyed" without a new "publishing" after)
    local last_publish last_close
    last_publish=$(docker logs "$CONTAINER_MTX" 2>&1 | grep "is publishing" | tail -1 || true)
    last_close=$(docker logs "$CONTAINER_MTX" 2>&1 | grep -E "destroyed|closed" | tail -1 || true)

    if [ -n "$last_publish" ] && [ -z "$last_close" ]; then
        # Stream was established, no close events
        return 0
    fi

    log "WARN: Streams not healthy (recent_publishers=$recent_publishers)"
    return 1
}

# --- Pre-restart Preflight ---

preflight_checks() {
    log "Running preflight checks before restart..."

    if ! check_internet; then
        log "BLOCKED: Internet is down — skipping restart"
        return 1
    fi

    if ! check_dns; then
        log "BLOCKED: DNS failing — skipping restart"
        return 1
    fi

    if ! check_wyze_api; then
        log "BLOCKED: Wyze servers unreachable — skipping restart"
        return 1
    fi

    log "Preflight checks passed"
    return 0
}

# --- Restart Logic ---

get_cooldown() {
    # Exponential backoff: 5min, 10min, 20min, 40min, then cap at 1hr
    local cooldown=$BASE_COOLDOWN
    local i=0
    while [ $i -lt $restart_count ] && [ $cooldown -lt $MAX_COOLDOWN ]; do
        cooldown=$((cooldown * 2))
        i=$((i + 1))
    done
    if [ $cooldown -gt $MAX_COOLDOWN ]; then
        cooldown=$MAX_COOLDOWN
    fi
    echo $cooldown
}

do_restart() {
    log "=== RESTARTING DOCKER STACK (restart #$((restart_count + 1))) ==="
    write_state "RESTARTING"

    cd "$COMPOSE_DIR"
    docker compose -f "$COMPOSE_FILE" down 2>&1 | while read -r line; do log "  down: $line"; done
    docker compose -f "$COMPOSE_FILE" up -d 2>&1 | while read -r line; do log "  up: $line"; done

    restart_count=$((restart_count + 1))
    last_restart_time=$(date +%s)
    consecutive_failures=0

    local cooldown
    cooldown=$(get_cooldown)
    log "Restart complete. Boot grace: ${BOOT_GRACE}s, next cooldown: ${cooldown}s"
}

# --- Main Loop ---

main() {
    log "=========================================="
    log "Cryze Stream Watchdog starting"
    log "Expected streams: $EXPECTED_STREAMS"
    log "Boot grace: ${BOOT_GRACE}s, check interval: ${CHECK_INTERVAL}s"
    log "Fail threshold: $FAIL_THRESHOLD, base cooldown: ${BASE_COOLDOWN}s"
    log "=========================================="

    # Initial boot grace period
    log "Entering boot grace period (${BOOT_GRACE}s)..."
    write_state "BOOT_WAIT"
    sleep "$BOOT_GRACE"

    while true; do
        write_state "MONITORING"

        # Step 1: Is the container even running?
        if ! check_container_running; then
            log "Container not running — attempting restart"
            if preflight_checks; then
                do_restart
                log "Entering boot grace period (${BOOT_GRACE}s)..."
                write_state "BOOT_WAIT"
                sleep "$BOOT_GRACE"
                continue
            else
                write_state "COOLDOWN_OUTAGE"
                log "External service issue — waiting ${OUTAGE_RECHECK}s before recheck"
                sleep "$OUTAGE_RECHECK"
                continue
            fi
        fi

        # Step 2: Is the app process running?
        if ! check_app_process; then
            consecutive_failures=$((consecutive_failures + 1))
            log "App not running (failure $consecutive_failures/$FAIL_THRESHOLD)"
        # Step 3: Are streams healthy?
        elif ! check_streams; then
            consecutive_failures=$((consecutive_failures + 1))
            log "Streams unhealthy (failure $consecutive_failures/$FAIL_THRESHOLD)"
        else
            # Everything is healthy
            if [ $consecutive_failures -gt 0 ]; then
                log "Streams recovered after $consecutive_failures failure(s)"
            fi
            consecutive_failures=0

            # Reset restart count after sustained healthy period (30 min)
            local now
            now=$(date +%s)
            if [ $restart_count -gt 0 ] && [ $((now - last_restart_time)) -gt 1800 ]; then
                log "Healthy for 30+ min, resetting restart counter"
                restart_count=0
            fi
        fi

        # Step 4: Have we exceeded the failure threshold?
        if [ $consecutive_failures -ge $FAIL_THRESHOLD ]; then
            log "Failure threshold reached ($consecutive_failures consecutive failures)"
            write_state "PREFLIGHT"

            if preflight_checks; then
                # Check cooldown from last restart
                local now cooldown elapsed
                now=$(date +%s)
                cooldown=$(get_cooldown)
                elapsed=$((now - last_restart_time))

                if [ $last_restart_time -gt 0 ] && [ $elapsed -lt $cooldown ]; then
                    local remaining=$((cooldown - elapsed))
                    log "Still in cooldown (${remaining}s remaining). Waiting..."
                    write_state "COOLDOWN_BACKOFF"
                    sleep "$CHECK_INTERVAL"
                    continue
                fi

                do_restart
                log "Entering boot grace period (${BOOT_GRACE}s)..."
                write_state "BOOT_WAIT"
                sleep "$BOOT_GRACE"
                continue
            else
                write_state "COOLDOWN_OUTAGE"
                log "External service issue — waiting ${OUTAGE_RECHECK}s before recheck"
                sleep "$OUTAGE_RECHECK"
                consecutive_failures=0  # Reset so we re-evaluate after outage clears
                continue
            fi
        fi

        sleep "$CHECK_INTERVAL"
    done
}

# Handle signals for clean shutdown
trap 'log "Watchdog stopping (signal received)"; exit 0' SIGTERM SIGINT

main "$@"
