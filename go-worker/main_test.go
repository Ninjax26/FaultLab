package main

import "testing"

func TestRetryDelay(t *testing.T) {
	cases := []struct{ attempt, want int }{{1, 2}, {2, 4}, {3, 8}, {20, 300}}
	for _, c := range cases {
		if got := retryDelay(c.attempt, 2, 300); got != c.want {
			t.Fatalf("attempt %d: got %d, want %d", c.attempt, got, c.want)
		}
	}
}
