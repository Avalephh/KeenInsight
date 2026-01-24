package analysis

import (
	"math"
	"testing"
)

func TestCalculateTPSFidelity(t *testing.T) {
	tests := []struct {
		name     string
		origin   []TimePoint
		replay   []TimePoint
		expected float64
	}{
		{
			name:     "Perfect match",
			origin:   []TimePoint{{1, 10}, {2, 20}},
			replay:   []TimePoint{{1, 10}, {2, 20}},
			expected: 1.0,
		},
		{
			name:     "Zero fidelity (all zero vs non-zero)",
			origin:   []TimePoint{{1, 10}},
			replay:   []TimePoint{{1, 0}},
			expected: 0.0, // RMSE=10, Mean=10, 1 - 10/10 = 0
		},
		{
			name:     "Half deviation",
			origin:   []TimePoint{{1, 10}},
			replay:   []TimePoint{{1, 5}},
			expected: 0.5, // RMSE=5, Mean=10, 1 - 5/10 = 0.5
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := CalculateTPSFidelity(tt.origin, tt.replay)
			if math.Abs(got-tt.expected) > 0.001 {
				t.Errorf("CalculateTPSFidelity() = %v, want %v", got, tt.expected)
			}
		})
	}
}

func TestCalculateLatencyFidelity(t *testing.T) {
	tests := []struct {
		name     string
		origin   []float64
		replay   []float64
		expected float64
	}{
		{
			name:     "Identical distribution",
			origin:   []float64{1, 2, 3, 4, 5},
			replay:   []float64{1, 2, 3, 4, 5},
			expected: 1.0,
		},
		{
			name:     "Shifted distribution very far",
			origin:   []float64{1, 2, 3},
			replay:   []float64{100, 200, 300},
			expected: 0.0, // Overlap is 0, max diff in CDF will be 1 at some point
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := CalculateLatencyFidelity(tt.origin, tt.replay)
			if math.Abs(got-tt.expected) > 0.001 {
				t.Errorf("CalculateLatencyFidelity() = %v, want %v", got, tt.expected)
			}
		})
	}
}

func TestCalculateStability(t *testing.T) {
	tests := []struct {
		name     string
		lags     []float64
		expected float64 // 1 / (1+stdDev)
	}{
		{
			name:     "Perfect stability (zero variance)",
			lags:     []float64{10, 10, 10}, // Mean=10, StdDev=0
			expected: 1.0,
		},
		{
			name:     "Some jitter",
			lags:     []float64{10, 12}, // Mean=11, Diff=(-1, 1), Sq=(1,1), Sum=2, Avg=1, StdDev=1
			expected: 0.5,               // 1 / (1+1) = 0.5
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := CalculateStability(tt.lags)
			if math.Abs(got-tt.expected) > 0.001 {
				t.Errorf("CalculateStability() = %v, want %v", got, tt.expected)
			}
		})
	}
}
