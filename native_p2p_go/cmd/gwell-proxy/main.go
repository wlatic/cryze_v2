package main

import (
	"log"
	"os"

	"github.com/wlatic/cryze_v2/native_p2p_go/pkg/gwell"
	"github.com/wlatic/cryze_v2/native_p2p_go/pkg/wyze"
)

func main() {
	log.SetFlags(log.Ltime | log.Lmicroseconds | log.Lshortfile)

	apiURL := envOr("CRYZE_API_URL", "http://localhost:8080")
	log.Printf("Cryze native P2P client starting")
	log.Printf("Using API: %s", apiURL)

	// Phase 1: Get camera list and tokens from Python API
	client := wyze.NewClient(apiURL)

	cameras, err := client.GetCameraList()
	if err != nil {
		log.Fatalf("Failed to get camera list: %v", err)
	}
	log.Printf("Found %d cameras: %v", len(cameras), cameras)

	// For each camera, get token and start P2P
	for _, camID := range cameras {
		info, err := client.GetDeviceInfo(camID)
		if err != nil {
			log.Printf("Failed to get info for %s: %v", camID, err)
			continue
		}
		log.Printf("Camera %s -> stream: %s, IP: %s", camID, info.StreamName, info.LanIP)

		token, err := client.GetCameraToken(camID)
		if err != nil {
			log.Printf("Failed to get token for %s: %v", camID, err)
			continue
		}
		log.Printf("Camera %s -> accessId: %s, token: %s...",
			camID, token.AccessID, token.AccessToken[:40])

		// Phase 2: Start P2P discovery
		go startCamera(camID, info, token)
	}

	// Keep running
	select {}
}

func startCamera(camID string, info *wyze.DeviceInfo, token *wyze.AccessCredential) {
	log.Printf("[%s] Starting P2P connection...", camID)

	// Phase 2: UDP Discovery - get P2P server list
	servers, err := gwell.DiscoverServers()
	if err != nil {
		log.Printf("[%s] Discovery failed: %v", camID, err)
		return
	}
	log.Printf("[%s] Discovered %d P2P servers", camID, len(servers))

	// Phase 3: Detect best server
	bestServer, err := gwell.DetectBestServer(servers)
	if err != nil {
		log.Printf("[%s] Server detection failed: %v", camID, err)
		return
	}
	log.Printf("[%s] Best P2P server: %s", camID, bestServer)

	// TODO: Phase 4: DTLS + Mars signaling
	// TODO: Phase 5: CALLING + Stream
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
