package metrics

import (
	"log"
	"net/http"

	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// StartServer starts the /metrics HTTP server on :9000 in a background goroutine.
// Call once from each service's main before entering its run loop.
func StartServer() {
	go func() {
		mux := http.NewServeMux()
		mux.Handle("/metrics", promhttp.Handler())
		log.Printf("Metrics server listening on :9000")
		if err := http.ListenAndServe(":9000", mux); err != nil {
			log.Printf("metrics server: %v", err)
		}
	}()
}
