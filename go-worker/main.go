// FaultLab's Go worker speaks the same PostgreSQL job protocol as the Python worker.
// It is intentionally small: this is a measured learning comparison, not a second control plane.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"reflect"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type job struct {
	id, queue, kind string
	payload         map[string]any
	attempt, max    int
}

type worker struct {
	pool      *pgxpool.Pool
	queue, id string
	lease     int
	heartbeat time.Duration
	poll      time.Duration
	retryBase int
	retryMax  int
}

func (w *worker) claim(ctx context.Context) (*job, error) {
	tx, err := w.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	var j job
	var payload []byte
	err = tx.QueryRow(ctx, `
		SELECT id::text, queue, kind, payload, attempt_count, max_attempts FROM jobs
		WHERE queue=$1 AND status IN ('pending','retry_pending') AND run_at <= now()
		ORDER BY priority DESC, run_at ASC, created_at ASC
		LIMIT 1 FOR UPDATE SKIP LOCKED`, w.queue,
	).Scan(&j.id, &j.queue, &j.kind, &payload, &j.attempt, &j.max)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err = json.Unmarshal(payload, &j.payload); err != nil {
		return nil, err
	}
	j.attempt++
	_, err = tx.Exec(ctx, `UPDATE jobs SET status='running', attempt_count=$2,
		lease_owner=$3, lease_expires_at=now()+make_interval(secs => $4),
		started_at=coalesce(started_at,now()), updated_at=now() WHERE id=$1::uuid`,
		j.id, j.attempt, w.id, w.lease)
	if err != nil {
		return nil, err
	}
	_, err = tx.Exec(ctx, `INSERT INTO job_attempts
		(id,job_id,attempt_number,worker_id,status,started_at)
		VALUES (gen_random_uuid(),$1::uuid,$2,$3,'running',now())`, j.id, j.attempt, w.id)
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &j, nil
}

func retryDelay(attempt, base, maximum int) int {
	delay := base
	for i := 1; i < attempt && delay < maximum; i++ {
		if delay > maximum/2 {
			return maximum
		}
		delay *= 2
	}
	if delay > maximum {
		return maximum
	}
	return delay
}

func (w *worker) reap(ctx context.Context) (int, error) {
	tx, err := w.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	rows, err := tx.Query(ctx, `SELECT id::text, attempt_count, max_attempts,
		cancellation_requested_at IS NOT NULL FROM jobs
		WHERE queue=$1 AND status='running' AND lease_expires_at < now()
		ORDER BY lease_expires_at LIMIT 100 FOR UPDATE SKIP LOCKED`, w.queue)
	if err != nil {
		return 0, err
	}
	type expired struct {
		id           string
		attempt, max int
		cancel       bool
	}
	var jobs []expired
	for rows.Next() {
		var j expired
		if err = rows.Scan(&j.id, &j.attempt, &j.max, &j.cancel); err != nil {
			rows.Close()
			return 0, err
		}
		jobs = append(jobs, j)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return 0, err
	}
	for _, j := range jobs {
		_, err = tx.Exec(ctx, `UPDATE job_attempts SET status='lease_expired',
			error='worker lease expired before completion', finished_at=now()
			WHERE job_id=$1::uuid AND attempt_number=$2 AND status='running'`, j.id, j.attempt)
		if err != nil {
			return 0, err
		}
		status := "retry_pending"
		if j.cancel {
			status = "cancelled"
		} else if j.attempt >= j.max {
			status = "dead"
		}
		_, err = tx.Exec(ctx, `UPDATE jobs SET status=$2::varchar, lease_owner=NULL,
			lease_expires_at=NULL, last_error='worker lease expired before completion',
			run_at=CASE WHEN $2::text='retry_pending' THEN now()+make_interval(secs => $3)
			ELSE run_at END,
			finished_at=CASE WHEN $2::text IN ('dead','cancelled') THEN now() ELSE finished_at END,
			updated_at=now() WHERE id=$1::uuid`,
			j.id, status, retryDelay(j.attempt, w.retryBase, w.retryMax))
		if err != nil {
			return 0, err
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, err
	}
	return len(jobs), nil
}

func (w *worker) renew(ctx context.Context, id string) (valid, cancel bool, err error) {
	err = w.pool.QueryRow(ctx, `UPDATE jobs SET lease_expires_at=now()+make_interval(secs => $3),
		updated_at=now() WHERE id=$1::uuid AND status='running' AND lease_owner=$2
		AND lease_expires_at > now() RETURNING cancellation_requested_at IS NOT NULL`,
		id, w.id, w.lease).Scan(&cancel)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, false, nil
	}
	return err == nil, cancel, err
}

func (w *worker) finish(ctx context.Context, j *job, outcome string, result map[string]any, handlerErr error) error {
	tx, err := w.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var cancel bool
	err = tx.QueryRow(ctx, `SELECT cancellation_requested_at IS NOT NULL FROM jobs
		WHERE id=$1::uuid AND status='running' AND lease_owner=$2
		AND lease_expires_at > now() FOR UPDATE`, j.id, w.id).Scan(&cancel)
	if errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("lease lost for %s", j.id)
	}
	if err != nil {
		return err
	}
	if outcome == "cancelled" && !cancel {
		return fmt.Errorf("job %s has no cancellation request", j.id)
	}
	if outcome == "failed" {
		if cancel {
			outcome = "cancelled"
		} else if j.attempt >= j.max {
			outcome = "dead"
		} else {
			outcome = "retry_pending"
		}
	}
	attemptStatus := "succeeded"
	if outcome == "retry_pending" || outcome == "dead" {
		attemptStatus = "failed"
	}
	if outcome == "cancelled" {
		attemptStatus = "cancelled"
	}
	errorText := ""
	if handlerErr != nil {
		errorText = handlerErr.Error()
	}
	if len(errorText) > 8000 {
		errorText = errorText[:8000]
	}
	_, err = tx.Exec(ctx, `UPDATE job_attempts SET status=$3, error=NULLIF($4,''), finished_at=now()
		WHERE job_id=$1::uuid AND attempt_number=$2`, j.id, j.attempt, attemptStatus, errorText)
	if err != nil {
		return err
	}
	resultJSON, err := json.Marshal(result)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE jobs SET status=$2::varchar, result=CASE WHEN $2::text='succeeded'
		THEN $3::jsonb ELSE result END, last_error=CASE WHEN $4='' THEN NULL ELSE $4 END,
		lease_owner=NULL, lease_expires_at=NULL,
		run_at=CASE WHEN $2::text='retry_pending' THEN now()+make_interval(secs => $5) ELSE run_at END,
		finished_at=CASE WHEN $2::text IN ('succeeded','dead','cancelled') THEN now() ELSE finished_at END,
		updated_at=now() WHERE id=$1::uuid`,
		j.id, outcome, string(resultJSON), errorText, retryDelay(j.attempt, w.retryBase, w.retryMax))
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func number(payload map[string]any, key string, fallback float64) (float64, error) {
	value, ok := payload[key]
	if !ok {
		return fallback, nil
	}
	num, ok := value.(float64)
	if !ok {
		return 0, fmt.Errorf("%s must be a number", key)
	}
	return num, nil
}

func (w *worker) handle(ctx context.Context, j *job) (map[string]any, error) {
	switch j.kind {
	case "benchmark-noop":
		return map[string]any{}, nil
	case "echo":
		return map[string]any{"echo": j.payload, "attempt_number": j.attempt, "worker_id": w.id}, nil
	case "flaky":
		count, err := number(j.payload, "fail_attempts", 1)
		if err != nil || count < 0 {
			return nil, fmt.Errorf("fail_attempts must be nonnegative")
		}
		if j.attempt <= int(count) {
			return nil, fmt.Errorf("intentional failure on attempt %d", j.attempt)
		}
		return map[string]any{"succeeded_on_attempt": j.attempt}, nil
	case "sleep", "sleep_uncooperative":
		seconds, err := number(j.payload, "seconds", 1)
		if err != nil || seconds < 0 || seconds > 30 {
			return nil, fmt.Errorf("sleep seconds must be between 0 and 30")
		}
		if j.kind == "sleep_uncooperative" {
			time.Sleep(time.Duration(seconds * float64(time.Second)))
		} else {
			deadline := time.NewTimer(time.Duration(seconds * float64(time.Second)))
			defer deadline.Stop()
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-deadline.C:
			}
		}
		return map[string]any{"slept_seconds": seconds}, nil
	case "record_once":
		key, ok := j.payload["business_key"].(string)
		if !ok || len(key) == 0 || len(key) > 255 {
			return nil, fmt.Errorf("business_key must be 1-255 characters")
		}
		value, ok := j.payload["value"].(map[string]any)
		if !ok {
			return nil, fmt.Errorf("value must be an object")
		}
		data, err := json.Marshal(value)
		if err != nil {
			return nil, err
		}
		effectCtx, effectCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer effectCancel()
		row := w.pool.QueryRow(effectCtx, `INSERT INTO business_effects (business_key,value)
			VALUES ($1,$2::jsonb) ON CONFLICT (business_key) DO NOTHING RETURNING business_key`, key, string(data))
		var insertedKey string
		err = row.Scan(&insertedKey)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, err
		}
		if errors.Is(err, pgx.ErrNoRows) {
			var existingJSON []byte
			if readErr := w.pool.QueryRow(effectCtx,
				`SELECT value FROM business_effects WHERE business_key=$1`, key,
			).Scan(&existingJSON); readErr != nil {
				return nil, readErr
			}
			var existing map[string]any
			if readErr := json.Unmarshal(existingJSON, &existing); readErr != nil {
				return nil, readErr
			}
			if !reflect.DeepEqual(existing, value) {
				return nil, fmt.Errorf("business_key was already used for a different effect value")
			}
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return map[string]any{"business_key": key, "effect_inserted": err == nil}, nil
	default:
		return nil, fmt.Errorf("no handler registered for %q", j.kind)
	}
}

func (w *worker) execute(ctx context.Context, j *job) error {
	handlerCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	state := make(chan string, 1)
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(w.heartbeat)
		defer ticker.Stop()
		for {
			select {
			case <-handlerCtx.Done():
				return
			case <-ticker.C:
				valid, requested, err := w.renew(ctx, j.id)
				if err != nil || !valid {
					state <- "lost"
					cancel()
					return
				}
				if requested {
					state <- "cancel"
					cancel()
					return
				}
			}
		}
	}()
	result, handlerErr := w.handle(handlerCtx, j)
	cancel()
	<-done
	if ctx.Err() != nil {
		return ctx.Err()
	}
	outcome := "succeeded"
	select {
	case reason := <-state:
		if reason == "lost" {
			return fmt.Errorf("lease lost for %s", j.id)
		}
		if handlerErr != nil {
			outcome = "cancelled"
		}
	default:
		if handlerErr != nil {
			outcome = "failed"
		}
	}
	return w.finish(ctx, j, outcome, result, handlerErr)
}

func (w *worker) run(ctx context.Context) error {
	for ctx.Err() == nil {
		if count, err := w.reap(ctx); err != nil {
			log.Printf("reaper: %v", err)
		} else if count > 0 {
			log.Printf("recovered %d expired leases", count)
		}
		j, err := w.claim(ctx)
		if err != nil {
			log.Printf("claim: %v", err)
		}
		if j != nil {
			if err := w.execute(ctx, j); err != nil {
				log.Printf("job %s: %v", j.id, err)
			}
			continue
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(w.poll):
		}
	}
	return nil
}

func intEnv(name string, fallback int) int {
	value, err := strconv.Atoi(os.Getenv(name))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func main() {
	databaseURL := flag.String("database-url", os.Getenv("FAULTLAB_DATABASE_URL"), "PostgreSQL URL")
	queue := flag.String("queue", "default", "queue to process")
	workerID := flag.String("worker-id", fmt.Sprintf("go-%d", os.Getpid()), "unique worker ID")
	flag.Parse()
	if *databaseURL == "" {
		log.Fatal("database-url is required")
	}
	url := strings.Replace(*databaseURL, "postgresql+asyncpg://", "postgresql://", 1)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	config, err := pgxpool.ParseConfig(url)
	if err != nil {
		log.Fatal(err)
	}
	config.MaxConns = int32(intEnv("FAULTLAB_GO_POOL_SIZE", 4))
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()
	w := &worker{
		pool: pool, queue: *queue, id: *workerID,
		lease:     intEnv("FAULTLAB_WORKER_LEASE_SECONDS", 30),
		heartbeat: time.Duration(intEnv("FAULTLAB_WORKER_HEARTBEAT_SECONDS", 10)) * time.Second,
		poll:      time.Duration(intEnv("FAULTLAB_WORKER_POLL_MILLISECONDS", 100)) * time.Millisecond,
		retryBase: intEnv("FAULTLAB_RETRY_BASE_SECONDS", 2),
		retryMax:  intEnv("FAULTLAB_RETRY_MAX_SECONDS", 300),
	}
	if w.heartbeat >= time.Duration(w.lease)*time.Second {
		log.Fatal("heartbeat must be shorter than lease")
	}
	log.Printf("Go worker started id=%s queue=%s", w.id, w.queue)
	if err := w.run(ctx); err != nil {
		log.Fatal(err)
	}
}
