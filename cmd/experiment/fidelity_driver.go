package main

import (
	"fmt"
	"math"
	"math/rand"
	"ruc-db-replay/internal/analysis"
)

// This driver simulates a replay scenario and calculates fidelity metrics.
// In a real environment, this would connect to the ReplayService and DB.

func main() {
	fmt.Println("Starting Fidelity Experiment...")

	// 1. Simulate Original Traffic (TPS series)
	// Assume a sine wave load pattern
	durationSec := 60
	originTPS := make([]analysis.TimePoint, durationSec)
	for i := 0; i < durationSec; i++ {
		tps := 1000.0 + 500.0*math.Sin(float64(i)*0.2)
		originTPS[i] = analysis.TimePoint{
			Timestamp: int64(i),
			Value:     tps,
		}
	}

	// 2. Simulate Replay Traffic (slightly noisy)
	replayTPS := make([]analysis.TimePoint, durationSec)
	for i := 0; i < durationSec; i++ {
		// Add some noise and slight lag
		noise := (rand.Float64() - 0.5) * 50.0 // +/- 25 TPS
		val := originTPS[i].Value + noise
		if val < 0 {
			val = 0
		}
		replayTPS[i] = analysis.TimePoint{
			Timestamp: int64(i),
			Value:     val,
		}
	}

	// 3. Calculate TPS Fidelity
	fidelityTPS := analysis.CalculateTPSFidelity(originTPS, replayTPS)
	fmt.Printf("TPS Fidelity (F_tps): %.4f\n", fidelityTPS)

	// 4. Simulate Latency Distributions
	// Origin: Exponential distribution, mean 5ms
	count := 10000
	originLatencies := make([]float64, count)
	for i := 0; i < count; i++ {
		originLatencies[i] = rand.ExpFloat64() * 5.0
	}

	// Replay: Exponential distribution, mean 5.2ms (slightly slower)
	replayLatencies := make([]float64, count)
	for i := 0; i < count; i++ {
		replayLatencies[i] = rand.ExpFloat64() * 5.2
	}

	// 5. Calculate Latency Fidelity
	fidelityLat := analysis.CalculateLatencyFidelity(originLatencies, replayLatencies)
	fmt.Printf("Latency Fidelity (F_lat): %.4f\n", fidelityLat)

	// 6. Simulate Schedule Stability
	// Lags should be close to 0
	lags := make([]float64, count)
	for i := 0; i < count; i++ {
		// Normal dist, mean 0.1ms, stddev 0.05ms
		lags[i] = rand.NormFloat64()*0.05 + 0.1
	}

	// 7. Calculate Stability
	stability := analysis.CalculateStability(lags)
	fmt.Printf("Stability (S_stab): %.4f\n", stability)

	// Generate LaTeX Table Row
	fmt.Println("\n--- LaTeX Table Row ---")
	fmt.Printf("TPS & %.2f\\\\%% & Latency & %.2f\\\\%% & Stability & %.2f\\\\%%\n", fidelityTPS*100, fidelityLat*100, stability*100)
}
