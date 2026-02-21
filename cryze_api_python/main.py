
import os
import sys
import logging
import asyncio
from typing import List, Optional, Dict, Any
import json
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from wyze_sdk import Client
from wyze_sdk.errors import WyzeClientError
from wyze_sdk.service.base import WpkNetServiceClient
import requests
import hashlib
import wyze_sdk.signature

# Monkey-patch: wyze_sdk's md5_string passes non-bytes to hashlib.md5()
def _patched_md5_string(self, body):
    if not isinstance(body, bytes):
        body = str(body).encode('utf-8')
    return hashlib.md5(body).hexdigest()

wyze_sdk.signature.RequestVerifier.md5_string = _patched_md5_string



# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("cryze_api")

app = FastAPI()

# CORS Middlewareation
WYZE_EMAIL = os.getenv("WYZE_EMAIL")
WYZE_PASSWORD = os.getenv("WYZE_PASSWORD")
API_ID = os.getenv("API_ID")
API_KEY = os.getenv("API_KEY")
MARS_URL = os.getenv("MARS_URL", "https://wyze-mars-service.wyzecam.com") # Default? Check C# config
MARS_REGISTER_GW_USER_ROUTE = os.getenv("MARS_REGISTER_GW_USER_ROUTE", "/plugin/mars/v2/regist_gw_user/")
# Original C# defaults: GW_BE1_, GW_GC1_, GW_GC2_. Using broader GW_ to catch all GWELL variants (DUO, etc.)
VALID_MARS_DEVICE_PREFIX = os.getenv("VALID_MARS_DEVICE_PREFIX", "GW_") # Comma separated

# Models
class CameraInfo(BaseModel):
    cameraId: str
    streamName: Optional[str] = None
    lanIp: Optional[str] = None

class AccessCredential(BaseModel):
    accessId: str
    accessToken: str

class CameraMessage(BaseModel):
    cameraId: str
    messageType: str
    path: str
    # data is handled via request body parsing as it might be dynamic

# Global State

# Manual IP Persistence
MANUAL_IPS_FILE = "data/manual_ips.json"

def load_manual_ips() -> Dict[str, str]:
    if os.path.exists(MANUAL_IPS_FILE):
        try:
            with open(MANUAL_IPS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load manual IPs: {e}")
    return {}

def save_manual_ips(ips: Dict[str, str]):
    try:
        with open(MANUAL_IPS_FILE, 'w') as f:
            json.dump(ips, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save manual IPs: {e}")

class ManualIPRequest(BaseModel):
    cameraId: str
    ip: str

# Update WyzeManager to include manual IP logic
class WyzeManager:
    def __init__(self):
        self.client: Optional[Client] = None
        self.cameras: Dict[str, CameraInfo] = {}
        self.manual_ips: Dict[str, str] = load_manual_ips()
        # NO token cache — tokens are ONE-TIME USE per the original C# implementation.
        # The IoTVideoSdk is very picky about this. Caching causes ASrv_tmpsubs_parse_fail (8020).
        self.supported_prefixes = [p.strip() for p in VALID_MARS_DEVICE_PREFIX.split(",") if p.strip()]
        self._ready = False  # Set True after startup prefetch completes

    def login(self):
        if not self.client:
            if not WYZE_EMAIL or not WYZE_PASSWORD:
                logger.error("WYZE_EMAIL or WYZE_PASSWORD not set")
                return
            
            try:
                logger.info(f"Attempting login for {WYZE_EMAIL}")
                self.client = Client(email=WYZE_EMAIL, password=WYZE_PASSWORD, key_id=API_ID, api_key=API_KEY)
                logger.info("Login successful")
            except WyzeClientError as e:
                logger.error(f"Failed to login: {e}")
                self.client = None
            except Exception as e:
                logger.exception(f"Unexpected error during login: {e}")
                self.client = None

    def refresh_cameras(self):
        if not self.client:
            self.login()
        
        if not self.client:
             logger.warning("Cannot refresh cameras, no client")
             return

        try:
            logger.info("Refreshing camera list...")
            response = self.client._api_client().get_object_list()
            
            if not response or not response.data:
                logger.error("Failed to get response from Wyze API")
                return

            # Wyze API returns nested structure: {'code': '1', 'data': {'device_list': [...]}}
            data_dict = response.data.get("data")
            
            if not data_dict or "device_list" not in data_dict:
                logger.error(f"Failed to get device_list from Wyze API response. Keys: {response.data.keys() if response.data else 'None'}")
                return

            devices = data_dict["device_list"]
            logger.info(f"Received {len(devices)} devices from Wyze API")

            new_cameras = {}
            for device in devices:
                mac = device.get("mac")
                nickname = device.get("nickname")
                product_type = device.get("product_type")
                product_model = device.get("product_model")

                # Check filtering
                if self.supported_prefixes:
                     if not any(mac.startswith(p) for p in self.supported_prefixes):
                        continue
                
                # Basic filter for cameras if no specific prefix set
                if not self.supported_prefixes:
                    if product_type in ["Lock", "Scale", "Band", "Plug", "Bulb", "Sensor", "Mesh"]:
                        continue
                    is_camera = "Camera" in (product_type or "") or "Doorbell" in (product_type or "") or "Cam" in (product_model or "")
                    if not is_camera:
                        continue

                safe_nickname = (nickname or mac).lower().replace(' ', '_')
                stream_name = f"live/{safe_nickname}"
                
                # IP Logic: Manual Override > Cloud IP > None
                cloud_ip = device.get("ip")
                final_ip = self.manual_ips.get(mac, cloud_ip)

                new_cameras[mac] = CameraInfo(
                    cameraId=mac,
                    streamName=stream_name,
                    lanIp=final_ip
                )
                
                logger.info(f"Found camera: {mac} ({nickname}) -> {stream_name} [IP: {final_ip} {'(Manual)' if mac in self.manual_ips else '(Cloud)'}]")

            self.cameras = new_cameras
            logger.info(f"Refreshed. Total cameras: {len(self.cameras)}")

        except Exception as e:
            logger.error(f"Failed to refresh cameras: {e}")
            logger.exception("Traceback:")

    def _fetch_token_from_mars(self, device_id: str) -> Optional[AccessCredential]:
        """Makes the actual external API call to Wyze Mars. This is slow (2-4s)."""
        if not self.client:
            self.login()

        if not self.client:
            return None

        try:
            wpk = WpkNetServiceClient(token=self.client._token, base_url=MARS_URL)
            path = MARS_REGISTER_GW_USER_ROUTE + device_id

            logger.info("Calling wpk.api_call...")
            resp = wpk.api_call(
                api_method=path,
                json={
                    "ttl_minutes": 10080,
                    "nonce": wpk.request_verifier.clock.nonce(),
                    "unique_id": wpk.phone_id
                },
                headers={
                    "appid": wpk.app_id
                },
                nonce=wpk.request_verifier.clock.nonce()
            )
            
            data_dict = getattr(resp, "data", None)
            if not data_dict and hasattr(resp, "_data"):
                data_dict = resp._data
            
            if data_dict and isinstance(data_dict, dict):
                 if "data" in data_dict:
                    data = data_dict["data"]

                 return AccessCredential(
                    accessId=data["accessId"],
                    accessToken=data["accessToken"]
                 )

            logger.error(f"Failed to get token response for {device_id}: {resp}")
            return None

        except Exception as e:
            logger.exception(f"Error fetching Mars token for {device_id}: {e}")
            return None


    def get_fresh_camera_token(self, device_id: str) -> Optional[AccessCredential]:
         return self._fetch_token_from_mars(device_id)

    def set_manual_ip(self, device_id: str, ip: str):
        self.manual_ips[device_id] = ip
        save_manual_ips(self.manual_ips)
        # Update in-memory camera immediately if present
        if device_id in self.cameras:
             # Create a copy to update
             cam = self.cameras[device_id]
             # Pydantic models are immutable-ish by default but we can replace
             new_cam = CameraInfo(cameraId=cam.cameraId, streamName=cam.streamName, lanIp=ip)
             self.cameras[device_id] = new_cam
        logger.info(f"Set manual IP for {device_id} to {ip}")


manager = WyzeManager()

@app.on_event("startup")
def startup_event():
    import threading
    def _init():
        try:
            manager.login()
            manager.refresh_cameras()
            manager._ready = True
            logger.info(f"API fully ready — {len(manager.cameras)} cameras discovered")
        except Exception as e:
            logger.exception(f"Startup init failed: {e}")
            manager._ready = True
    threading.Thread(target=_init, daemon=True).start()


@app.get("/health")
def health():
    if not manager._ready:
        raise HTTPException(status_code=503, detail="API starting up, cameras not yet discovered")
    return {"status": "ok", "cameras": len(manager.cameras)}


@app.get("/Camera/CameraList")
def get_camera_list():
    return list(manager.cameras.keys())

@app.get("/Camera/DeviceInfo")
def get_device_info(deviceId: str):
    if deviceId in manager.cameras:
        return manager.cameras[deviceId]
    raise HTTPException(status_code=404, detail="Camera not found")

@app.get("/Camera/CameraToken")
def get_camera_token_endpoint(deviceId: str):
    token = manager.get_fresh_camera_token(deviceId)
    if token:
        return token
    raise HTTPException(status_code=500, detail=f"Failed to fetch token for {deviceId}")

@app.post("/Camera/SetManualIP")
def set_manual_ip(req: ManualIPRequest):
    manager.set_manual_ip(req.cameraId, req.ip)
    return {"status": "updated", "cameraId": req.cameraId, "ip": req.ip}

@app.post("/Camera/GetAllSupportedCameras")
def trigger_refresh_cameras(background_tasks: BackgroundTasks):
    background_tasks.add_task(manager.refresh_cameras)
    return {"status": "refresh_queued"}

# Event Storage
camera_messages = {}

@app.post("/CameraMessage")
async def receive_camera_message(
    cameraId: str = Query(...), 
    messageType: str = Query(...), 
    path: Optional[str] = Query(None),
    request: Request = None
):
    body_bytes = await request.body()
    try:
        data = body_bytes.decode('utf-8')
    except UnicodeDecodeError:
        data = str(body_bytes)

    logger.info(f"CameraMessage: {cameraId} [{messageType}] {path}")

    if cameraId not in camera_messages:
        camera_messages[cameraId] = {}

    if messageType == "MSG_TYPE_PRO_CONST":
        camera_messages[cameraId][messageType] = data
    elif messageType == "MSG_TYPE_PRO_READONLY":
        key = f"{messageType}::{path}" if path else messageType
        camera_messages[cameraId][key] = data
    elif messageType == "MSG_TYPE_PRO_WRITABLE":
        try:
            payload = json.loads(data)
            if isinstance(payload, dict):
                for sub_key, sub_val in payload.items():
                     key = f"{messageType}::{path}::{sub_key}"
                     camera_messages[cameraId][key] = str(sub_val)
            else:
                 camera_messages[cameraId][f"{messageType}::{path}"] = data
        except json.JSONDecodeError:
            camera_messages[cameraId][f"{messageType}::{path}"] = data
    else:
        key = f"{messageType}::{path}" if path else messageType
        camera_messages[cameraId][key] = data
    
    return {"status": "received"}

@app.get("/messages")
def get_messages():
    return camera_messages

@app.get("/", response_class=HTMLResponse)
def dashboard():
    
    android_ip = os.getenv("CRYZE_ANDROID_IP")
    rtsp_port_env = os.getenv("RTSP_PORT_EXTERNAL", "18554")
    
    if android_ip:
        rtsp_host_js_logic = f"'{android_ip}'"
        rtsp_port_js_logic = "'8554'"
    else:
        rtsp_host_js_logic = "location.hostname"
        rtsp_port_js_logic = f"'{rtsp_port_env}'"

    token_data = {}
    for cam_id in manager.cameras:
        token_data[cam_id] = "fetched fresh per-request"
    
    token_json = json.dumps(token_data)
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>Wyze Streaming Dashboard</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 20px; background: #f0f2f5; }}
                .container {{ max-width: 1200px; margin: 0 auto; }}
                .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
                .header-actions {{ display: flex; gap: 10px; }}
                .btn {{ background: #2563eb; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 0.9em; }}
                .btn:hover {{ background: #1d4ed8; }}
                .btn:disabled {{ background: #9ca3af; cursor: wait; }}
                .camera-card {{ background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px; padding: 20px; }}
                .camera-header {{ display: flex; justify-content: space-between; border-bottom: 1px solid #eee; padding-bottom: 10px; margin-bottom: 10px; }}
                .camera-id {{ font-weight: bold; font-size: 1.1em; }}
                .token-status {{ font-size: 0.85em; padding: 2px 8px; border-radius: 10px; }}
                .token-ok {{ background: #e6f4ea; color: #1e7e34; }}
                .token-missing {{ background: #fce8e6; color: #c5221f; }}
                .event {{ font-family: monospace; font-size: 0.85em; background: #f8f9fa; padding: 5px; margin: 2px 0; border-radius: 3px; word-break: break-all; }}
                .event-key {{ color: #d63384; font-weight: bold; }}
                .status-badge {{ background: #e6f4ea; color: #1e7e34; padding: 2px 8px; border-radius: 10px; font-size: 0.8em; }}
                .ip-badge {{ background: #e0f2fe; color: #0369a1; padding: 2px 8px; border-radius: 10px; font-size: 0.8em; margin-left: 10px; cursor: pointer; }}
                .ip-badge:hover {{ background: #bae6fd; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Wyze Local Streaming</h1>
                    <div class="header-actions">
                        <button class="btn" onclick="refreshCameras(this)">🔄 Refresh Cameras</button>
                    </div>
                </div>
                <div id="cameras">Loading cameras...</div>
            </div>
            <script>
                const RTSP_HOST = {rtsp_host_js_logic};
                const RTSP_PORT = {rtsp_port_js_logic};
                const TOKEN_STATUS = {token_json};

                function refreshCameras(btn) {{
                    btn.disabled = true; btn.textContent = '⏳ Refreshing...';
                    fetch('/Camera/GetAllSupportedCameras', {{method: 'POST'}})
                        .then(r => r.json())
                        .then(() => {{ btn.textContent = '✓ Queued'; setTimeout(() => location.reload(), 5000); }})
                        .catch(() => {{ btn.disabled = false; btn.textContent = '❌ Failed'; }});
                }}

                function setIp(mac, currentIp) {{
                    const newIp = prompt(`Set Manual LAN IP for ${{mac}}:\\n(Leave empty to reset to Cloud IP)`, currentIp || '');
                    if (newIp !== null) {{
                        fetch('/Camera/SetManualIP', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify({{cameraId: mac, ip: newIp}})
                        }}).then(r => r.json()).then(() => location.reload());
                    }}
                }}

                async function load() {{
                    try {{
                        const [camIds, msgs] = await Promise.all([
                            fetch('/Camera/CameraList').then(r => r.json()),
                            fetch('/messages').then(r => r.json())
                        ]);
                        
                        const container = document.getElementById('cameras');
                        
                        if (camIds.length === 0) {{
                            container.innerHTML = '<div class="camera-card">No cameras found. Check logs.</div>';
                            return;
                        }}

                        const cams = await Promise.all(camIds.map(id =>
                            fetch('/Camera/DeviceInfo?deviceId=' + encodeURIComponent(id)).then(r => r.json())
                        ));

                        container.innerHTML = cams.map(cam => {{
                            const mac = cam.cameraId;
                            const streamName = cam.streamName || ('live/' + mac);
                            const camMsgs = msgs[mac] || {{}};
                            const msgHtml = Object.entries(camMsgs).map(([k, v]) => 
                                `<div class="event"><span class="event-key">${{k}}</span>: ${{v}}</div>`
                            ).join('');
                            
                            const rtspUrl = `rtsp://${{RTSP_HOST}}:${{RTSP_PORT}}/${{streamName}}`;
                            const tokenInfo = TOKEN_STATUS[mac];
                            const tokenClass = tokenInfo && tokenInfo.startsWith('✓') ? 'token-ok' : '';
                            
                            return `
                                <div class="camera-card">
                                    <div class="camera-header">
                                        <div style="display: flex; align-items: center;">
                                            <div class="camera-id">${{mac}}</div>
                                            <div class="ip-badge" onclick="setIp('${{mac}}', '${{cam.lanIp || ''}}')">
                                                IP: ${{cam.lanIp || 'Unknown (Set Manual)'}} ✏️
                                            </div>
                                        </div>
                                        <div>
                                            <span class="status-badge">Connected</span>
                                        </div>
                                    </div>
                                    <div style="margin-bottom: 15px; padding: 10px; background: #e2e3e5; border-radius: 4px;">
                                        <strong>Stream URL:</strong> 
                                        <code style="font-family: monospace; user-select: all;">${{rtspUrl}}</code>
                                    </div>
                                    <div class="events-section">
                                        <h4>Device Events</h4>
                                        ${{msgHtml || '<div>No events received yet</div>'}}
                                    </div>
                                </div>
                            `;
                        }}).join('');

                    }} catch (e) {{
                        console.error(e);
                    }}
                }}
                load();
                setInterval(load, 3000);
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/Camera/AddOrUpdate")
def add_or_update_camera(camera: CameraInfo):
    manager.cameras[camera.cameraId] = camera
    return {"status": "updated"}

@app.post("/Camera/Delete")
def delete_camera(camera: CameraInfo):
    if camera.cameraId in manager.cameras:
        del manager.cameras[camera.cameraId]
    return {"status": "deleted"}

