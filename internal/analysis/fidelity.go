package analysis

import (
	"math"
	"sort"
)

// TimePoint represents a data point in a time series (e.g., TPS at a specific time)
type TimePoint struct {
	Timestamp int64   // Logical timestamp or offsets
	Value     float64 // Metric value (e.g., TPS)
}

// CalculateTPSFidelity calculates the Throughput Fidelity (F_tps) using NRMSE.
// It assumes that origin and replay sequences are already aligned by logical time.
func CalculateTPSFidelity(origin []TimePoint, replay []TimePoint) float64 {
	if len(origin) == 0 || len(replay) == 0 {
		return 0.0
	}

	// Ensure same length for comparison (truncate to shorter)
	n := len(origin)
	if len(replay) < n {
		n = len(replay)
	}

	var sumSquaredError float64
	var sumOrigin float64

	for i := 0; i < n; i++ {
		diff := origin[i].Value - replay[i].Value
		sumSquaredError += diff * diff
		sumOrigin += origin[i].Value
	}

	rmse := math.Sqrt(sumSquaredError / float64(n))
	meanOrigin := sumOrigin / float64(n)

	if meanOrigin == 0 {
		if rmse == 0 {
			return 1.0 // Both are zero, perfect match
		}
		return 0.0 // Avoid division by zero
	}

	fidelity := 1.0 - (rmse / meanOrigin)
	return fidelity
}

// CalculateLatencyFidelity calculates Latency Fidelity (F_lat) using Kolmogorov-Smirnov test.
func CalculateLatencyFidelity(originLatencies []float64, replayLatencies []float64) float64 {
	if len(originLatencies) == 0 || len(replayLatencies) == 0 {
		return 0.0
	}

	// Sort both arrays to compute ECDF
	// Create copies to avoid modifying original data
	src := make([]float64, len(originLatencies))
	copy(src, originLatencies)
	sort.Float64s(src)

	tgt := make([]float64, len(replayLatencies))
	copy(tgt, replayLatencies)
	sort.Float64s(tgt)

	// Compute KS statistic D
	dMax := 0.0
	i, j := 0, 0
	nSrc, nTgt := len(src), len(tgt)

	for i < nSrc && j < nTgt {
		// Current values
		val := src[i]
		if tgt[j] < val {
			val = tgt[j]
		}

		// Advance pointers for values <= val
		for i < nSrc && src[i] <= val {
			i++
		}
		for j < nTgt && tgt[j] <= val {
			j++
		}

		// Compute CDFs
		cdfSrc := float64(i) / float64(nSrc)
		cdfTgt := float64(j) / float64(nTgt)

		diff := math.Abs(cdfSrc - cdfTgt)
		if diff > dMax {
			dMax = diff
		}
	}

	// Check tail if one array finishes first (though loop logic covers max diff usually)
	// Theoretically KS is sup|F1(x)-F2(x)| over all x.
	// If one array isn't finished, the CDF of the finished one remains 1.0.
	// But since we iterate based on "next smallest value", if one finishes,
	// the remaining values of the other will drive its CDF to 1.0, eventually matching.

	// Fidelity = 1 - D_KS
	return 1.0 - dMax
}

// CalculateStability calculates Stability (S_stab) based on schedule lag.
// lags: slice of (ActualTime - ScheduledTime) in relevant unit (e.g. seconds or ms)
func CalculateStability(lags []float64) float64 {
	if len(lags) == 0 {
		return 0.0
	}

	var sum float64
	for _, v := range lags {
		sum += v
	}
	mean := sum / float64(len(lags))

	var sumSqDiff float64
	for _, v := range lags {
		diff := v - mean
		sumSqDiff += diff * diff
	}
	stdDev := math.Sqrt(sumSqDiff / float64(len(lags)))

	// S_stab = 1 / (1 + sigma)
	return 1.0 / (1.0 + stdDev)
}
