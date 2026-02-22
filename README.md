This is a continuation and optimization of the original Cryze V2 addon.

Hello!

This is a RTSP server for WYZE cameras of the GWELL variety.

## preface
THANK YOU to Carson Loyal (carTloyal123) for the libraries to connect and get streams and pedroSG94 for RTSP related android libraries. I used the following repos:
- [cryze-android](https://github.com/carTloyal123/cryze-android) - the library for connecting to the cameras
- [cryze](https://github.com/carTloyal123/cryze) - scripts for getting tokens, capturing raw stream contents
- [RootEncoder](https://github.com/pedroSG94/RootEncoder) - library for streaming RTSP
- [MediaMTX](https://github.com/bluenviron/mediamtx) - handles (re)serving the content that the android component remuxes

## Features
- *uses local streaming by default*
- provides one stream in MediaMTX, configured at the UI (port 8080)
- scrapes your account at startup and automatically adds all supported wyze gwell-based cameras
- runs well in docker
- automatic network fix for macvlan LAN streaming
- smart watchdog service with auto-restart and outage detection

## Quick Start

### 1. Host Requirements

This runs Android-in-Docker via [Redroid](https://github.com/remote-android/redroid-doc), which needs specific kernel support. It does **not** work on Docker Desktop (Windows/Mac), WSL2, or most cloud VMs.

**Recommended OS:** [Debian 12 Minimal](https://www.debian.org/CD/netinst/) (~300MB installed, smallest stable option with full kernel module support)

**Minimum specs:**
| Resource | Cryze Only | Full Stack (+ Frigate + Wyze Bridge) |
|---|---|---|
| CPU | 2 cores | 4+ cores |
| RAM | 4 GB | 8+ GB |
| Disk | 16 GB | 64+ GB (Frigate recordings) |

**Proxmox VM settings:**
- CPU Type: `host` (required for Redroid)
- Machine: `q35`
- BIOS: `OVMF (UEFI)` or `SeaBIOS`
- Enable: Nesting (Options → Processor → Enable Nesting)

**Supported hosts:**
- Proxmox VM (Debian 12 Minimal) ✅ recommended
- Bare-metal Linux server ✅
- Proxmox LXC with nesting ✅ (advanced)

**Kernel setup (one-time, after installing Debian):**
```bash
# Install Docker
apt update && apt install -y curl git
curl -fsSL https://get.docker.com | sh

# Install kernel headers and load binder module
apt install -y linux-headers-$(uname -r)
modprobe binder_linux devices=binder,hwbinder,vndbinder
echo "binder_linux" >> /etc/modules

# Verify
ls /dev/binderfs/  # should show binder, hwbinder, vndbinder
```

If your kernel doesn't have `binder_linux`, follow the [Redroid kernel guide](https://github.com/remote-android/redroid-doc/blob/master/deploy/README.md).

### 2. Clone & Configure

```bash
git clone https://github.com/wlatic/cryze_v2.git
cd cryze_v2
cp .env.example .env
nano .env  # Edit the values below
```

**`.env` values to set:**
```env
# Wyze account (required)
WYZE_EMAIL=your@email.com
WYZE_PASSWORD=your_password
WYZE_KEY_ID=your_key_id        # from https://developer-api-console.wyze.com/
WYZE_API_KEY=your_api_key

# Network — adjust for your LAN
LAN_INTERFACE=ens18             # run 'ip link' to find yours
LAN_SUBNET=10.10.0.0/16        # your LAN subnet
LAN_GATEWAY=10.10.0.1          # your router IP
LAN_IP_RANGE=10.10.20.208/29   # unused IP range for containers
CONTAINER_IP=10.10.20.215      # IP for the Android container
API_IP=10.10.20.216             # IP for the API container
EXPECTED_STREAMS=2              # number of cameras
```

### 3. Choose Your Deployment

**Option A: Cryze only** (GW cameras only)
```bash
docker compose -f docker-compose.macvlan-only.yml up -d --build
```

**Option B: Full stack** (GW + non-GW cameras + Frigate NVR)

Add the extra IPs to your `.env`:
```env
FRIGATE_IP=10.10.20.210       # Frigate web UI
WYZE_BRIDGE_IP=10.10.20.211   # docker-wyze-bridge
```

Edit `frigate/config/config.yml` with your camera names and IPs, then:
```bash
docker compose -f docker-compose.frigate.yml up -d --build
```

This starts:
| Service | Purpose | Cameras |
|---|---|---|
| **Cryze** | RTSP streaming | GW cameras (OG, Doorbell Pro) |
| **docker-wyze-bridge** | RTSP streaming | Non-GW cameras (V3, Pan, Outdoor, etc) |
| **Frigate** | Recording + AI detection | All cameras |

Access points:
- Frigate UI: `http://<FRIGATE_IP>:5000`
- Wyze Bridge UI: `http://<WYZE_BRIDGE_IP>:5000`
- Cryze streams: `rtsp://<CONTAINER_IP>:8554/live/<name>`
- Wyze Bridge streams: `rtsp://<WYZE_BRIDGE_IP>:8554/<name>`

Wait ~2-3 minutes for Android to boot and cameras to connect.

### 4. Install Watchdog (recommended)

The watchdog monitors your streams and auto-restarts if they fail. It won't restart during internet or Wyze outages.

```bash
cp cryze-watchdog.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cryze-watchdog
```

**Watchdog features:**
- Checks streams every 60s via MediaMTX API
- 3 consecutive failures before restarting (~3 min tolerance)
- Pre-restart checks: internet, DNS, Wyze servers — won't restart during outages
- Exponential backoff: 5→10→20→40 min→1hr cap
- Auto-resets after 30 min of stable streams
- Debug: `cat /tmp/cryze-watchdog-state` or `journalctl -u cryze-watchdog`

**Watchdog config** (optional, in `watchdog.env`):
```bash
cp watchdog.env.example watchdog.env
nano watchdog.env  # Override timing, thresholds, etc.
```

## Prereqs
- An x86 machine. I am using libhoudini in `redroid` to make the cryze android app work with the binaries for getting connections. This avoids the overhead of qemu or other android emulators.
- a kernel compatible with `redroid`. follow [this guide](https://github.com/remote-android/redroid-doc/blob/master/deploy/README.md), optionally starting a redroid container to confirm it works
- Wyze GWELL cameras. I've tested with `GW_GC1` (Wyze Cam OG) and `GW_BE1` (Wyze Cam Doorbell Pro	), 3 concurrent streams seems stable.

## Camera configurations
The configuration is one of three ways:
1) webui, port 8080
2) edit the json yourself
3) just let the app scrape your cameras
4) set the WyzeSdkServiceConfiguration__ValidMarsDevicePrefix environment variable `=device1,device2,etc` in your docker-compose on the API service and restart. on reboot, it will only scrape and add devices matching whatever you set (as a prefix, which can also be the entire device name)
5) if you do not set your own route, the default route is live/nickname - where nickname is all lowecase and spaces are underscores

## Webapp
Account Settings:

![a page with settings for your wyze account](images/account_settings.png)

Camera Editor:
![list of cameras and the stream subpath the streams will be sent to](images/camera_editor.png)

Message Viewer:
![A view of the json structure of messages from a camera](images/messages_viewer.png)

The homepage might soon have a live view of your cameras.

## Development
I am using Android Studio for the android app, and just attaching to my remote docker-hosted `redroid` container (`adb connect [arch box ip address]:5555`). debugging/remote builds work, but container reboots will not persist your `/data` partition, so be sure to rebuild/restart with updated sources. If you need to stop the running cryze version, `adb shell setprop breakloop 1` will stop the loop that ensures cryze is running. You'll need to uninstall the current version to install your local build and android studio isn't very good at figuring this out.

If you choose to build the android app locally, you can override the rtsp server and cryze_api URI.
- `CRYZE_RTSP_SERVER=localhost` - if your using the in-container rtsp server, its at localhost
- `CRYZE_BACKEND_URL=http://cryze_api:8080` - You could totally point this to your dev machine

The api solution, you can just `dotnet watch run` or if you don't want to do that, `docker build -t cryzeapi . ; docker run --rm -it --env-file=..\.env -v=".\data.json:/data/data.json" -p="8080:8080" cryzeapi`

## Support
File an issue with as much detail as you can. I have limited time to work on this, but I'll try to help. I've replaced my wyze-gwell cameras with tapo c120's for better low-light support and native RTSP, but I will attempt to repro anything you might run into.

## HELP NEEDED (backlog?)
- move to the latest/a newe version of IoTVideoSDK
- render events on the API server in some sort of view, there's some cool data in the event stream. I made classes to deserialize the events into.

## License
- All files in `cryze_android_app/app/src/main/java/com/pedro` that are copied works from `RootEncoder` and remain licensed Apache v2 per the code's source repositories (linked above) My changes are licensed GPLv3 (RTSP codec extensions) unless it can be proven my changes are incompatible, in which case they retain the Apache v2 license from `RootEncoder`
- Remaining files not from named repos above, or missing copyright headers are licenses GPL v3, see the copy of that license located [here](LICENSE)
- IoTVideoSDK is complicated. I rewrote _most_ of the SDK from scratch using only the needed JNI extensions for the binary blobs to be happy. My changes are also GPLv3, the rest are public domain (untouched java primairly).
- I do not own or claim any personal stake to the NDK libraries this uses. They are readily available on the internet in several forms.
