package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	goredis "github.com/redis/go-redis/v9"

	"github.com/preraku/atbatwatch/internal/metrics"
)

const (
	deliveriesStream = "events:deliveries"
	deliveryGroup    = "delivery-group"
	deliveryConsumer = "delivery-1"
)

var webhookClient = &http.Client{Timeout: 10 * time.Second}

var (
	notificationsDeliveredTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "notifications_delivered_total",
		Help: "Total Discord webhook notifications successfully delivered.",
	})

	discordWebhookRequestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "discord_webhook_requests_total",
		Help: "Total Discord webhook attempts by status.",
	}, []string{"status"})

	discordWebhookDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "discord_webhook_duration_seconds",
		Help:    "Discord webhook call latency.",
		Buckets: prometheus.DefBuckets,
	})

	deliveryErrorsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "delivery_errors_total",
		Help: "Total delivery errors by type.",
	}, []string{"type"})
)

func main() {
	args := os.Args[1:]
	if len(args) > 0 && args[0] == "atbatwatch" {
		args = args[1:]
	}
	if len(args) == 0 {
		printHelp()
		os.Exit(0)
	}
	switch args[0] {
	case "run-delivery":
		metrics.StartServer()
		if err := runDelivery(); err != nil {
			log.Fatalf("run-delivery: %v", err)
		}
	case "delivery-once":
		if err := deliveryOnce(); err != nil {
			log.Fatalf("delivery-once: %v", err)
		}
	case "--help", "-h", "help":
		printHelp()
	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n", args[0])
		printHelp()
		os.Exit(1)
	}
}

func printHelp() {
	fmt.Println("Usage: atbatwatch <command>")
	fmt.Println()
	fmt.Println("Available commands:")
	fmt.Println("  run-delivery   Run the delivery worker (loop)")
	fmt.Println("  delivery-once  Process all pending deliveries once and exit")
}

// pgxDSN converts the SQLAlchemy-style DATABASE_URL (postgresql+asyncpg://...)
// to the standard postgresql:// DSN that pgx expects.
func pgxDSN(raw string) string {
	return strings.Replace(raw, "postgresql+asyncpg://", "postgresql://", 1)
}

func connect(ctx context.Context) (*pgxpool.Pool, *goredis.Client, error) {
	dbURL := pgxDSN(os.Getenv("DATABASE_URL"))
	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		return nil, nil, fmt.Errorf("postgres: %w", err)
	}

	redisURL := os.Getenv("REDIS_URL")
	opts, err := goredis.ParseURL(redisURL)
	if err != nil {
		pool.Close()
		return nil, nil, fmt.Errorf("redis url: %w", err)
	}
	rdb := goredis.NewClient(opts)

	return pool, rdb, nil
}

func ensureGroup(ctx context.Context, rdb *goredis.Client) {
	rdb.XGroupCreateMkStream(ctx, deliveriesStream, deliveryGroup, "0")
}

// reclaimPEL reclaims messages idle for >30s from a previous crashed consumer.
func reclaimPEL(ctx context.Context, pool *pgxpool.Pool, rdb *goredis.Client) {
	cursor := "0-0"
	for {
		msgs, next, err := rdb.XAutoClaim(ctx, &goredis.XAutoClaimArgs{
			Stream:   deliveriesStream,
			Group:    deliveryGroup,
			Consumer: deliveryConsumer,
			MinIdle:  30 * time.Second,
			Start:    cursor,
			Count:    100,
		}).Result()
		if err != nil {
			log.Printf("delivery: xautoclaim: %v", err)
			return
		}
		for _, msg := range msgs {
			processOne(ctx, pool, rdb, msg.ID, msg.Values)
		}
		if next == "0-0" || len(msgs) == 0 {
			return
		}
		cursor = next
	}
}

// formatContent builds the byte-identical Discord content string.
func formatContent(fields map[string]interface{}) string {
	playerName := str(fields["player_name"])
	state := str(fields["state"])
	awayTeam := str(fields["away_team_name"])
	homeTeam := str(fields["home_team_name"])
	inning, _ := strconv.Atoi(str(fields["inning"]))
	inningHalf := str(fields["inning_half"])
	outs, _ := strconv.Atoi(str(fields["outs"]))

	var label string
	if state == "at_bat" {
		label = "⚾ **AT BAT**"
	} else {
		label = "🔄 **ON DECK**"
	}

	outWord := "outs"
	if outs == 1 {
		outWord = "out"
	}

	var suffix string
	if inning != 0 {
		// em-dash U+2014
		suffix = fmt.Sprintf(" — %s %d, %d %s", inningHalf, inning, outs, outWord)
	}

	return fmt.Sprintf("%s: **%s** (%s @ %s%s)", label, playerName, awayTeam, homeTeam, suffix)
}

func str(v interface{}) string {
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

// alreadySent checks notification_log for (event_id, user_id).
func alreadySent(ctx context.Context, pool *pgxpool.Pool, eventID string, userID int64) (bool, error) {
	var exists bool
	err := pool.QueryRow(ctx,
		"SELECT EXISTS(SELECT 1 FROM notification_log WHERE event_id=$1 AND user_id=$2)",
		eventID, userID,
	).Scan(&exists)
	return exists, err
}

// logSent inserts a notification_log row; ON CONFLICT DO NOTHING handles races.
func logSent(ctx context.Context, pool *pgxpool.Pool, eventID string, userID, playerID int64, state string) error {
	_, err := pool.Exec(ctx,
		`INSERT INTO notification_log (event_id, user_id, player_id, state, status)
		 VALUES ($1, $2, $3, $4, 'sent')
		 ON CONFLICT ON CONSTRAINT uq_notification_log_event_user DO NOTHING`,
		eventID, userID, playerID, state,
	)
	return err
}

// postWebhook sends the Discord webhook POST. Returns error on non-2xx.
func postWebhook(webhookURL, content string) error {
	body, _ := json.Marshal(map[string]string{"content": content})
	start := time.Now()
	resp, err := webhookClient.Post(webhookURL, "application/json", bytes.NewReader(body))
	discordWebhookDuration.Observe(time.Since(start).Seconds())
	if err != nil {
		discordWebhookRequestsTotal.WithLabelValues("error").Inc()
		return err
	}
	defer resp.Body.Close()
	discordWebhookRequestsTotal.WithLabelValues(strconv.Itoa(resp.StatusCode)).Inc()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("webhook returned %d", resp.StatusCode)
	}
	return nil
}

// processOne handles a single delivery message.
func processOne(ctx context.Context, pool *pgxpool.Pool, rdb *goredis.Client, msgID string, fields map[string]interface{}) {
	eventID := str(fields["event_id"])
	userID, _ := strconv.ParseInt(str(fields["user_id"]), 10, 64)
	playerID, _ := strconv.ParseInt(str(fields["player_id"]), 10, 64)
	state := str(fields["state"])
	webhookURL := str(fields["webhook_url"])

	sent, err := alreadySent(ctx, pool, eventID, userID)
	if err != nil {
		log.Printf("idempotency check failed for msg %s: %v", msgID, err)
		return
	}
	if sent {
		rdb.XAck(ctx, deliveriesStream, deliveryGroup, msgID)
		return
	}

	content := formatContent(fields)
	if err := postWebhook(webhookURL, content); err != nil {
		log.Printf("Discord delivery failed for user %d: %v", userID, err)
		deliveryErrorsTotal.WithLabelValues("webhook").Inc()
		return // don't ACK — retain for retry
	}
	notificationsDeliveredTotal.Inc()

	if err := logSent(ctx, pool, eventID, userID, playerID, state); err != nil {
		log.Printf("log_sent failed for msg %s: %v", msgID, err)
		deliveryErrorsTotal.WithLabelValues("db").Inc()
		// We already sent the webhook; best-effort log. Still ACK.
	}

	rdb.XAck(ctx, deliveriesStream, deliveryGroup, msgID)
}

func deliveryOnce() error {
	ctx := context.Background()
	pool, rdb, err := connect(ctx)
	if err != nil {
		return err
	}
	defer pool.Close()
	defer rdb.Close()

	ensureGroup(ctx, rdb)
	reclaimPEL(ctx, pool, rdb)

	results, err := rdb.XReadGroup(ctx, &goredis.XReadGroupArgs{
		Group:    deliveryGroup,
		Consumer: deliveryConsumer,
		Streams:  []string{deliveriesStream, ">"},
		Count:    100,
		Block:    -1, // -1 = no BLOCK arg = non-blocking
	}).Result()
	if err != nil && err != goredis.Nil {
		return fmt.Errorf("xreadgroup: %w", err)
	}

	for _, stream := range results {
		for _, msg := range stream.Messages {
			processOne(ctx, pool, rdb, msg.ID, msg.Values)
		}
	}
	return nil
}

func runDelivery() error {
	ctx := context.Background()
	pool, rdb, err := connect(ctx)
	if err != nil {
		return err
	}
	defer pool.Close()
	defer rdb.Close()

	ensureGroup(ctx, rdb)
	reclaimPEL(ctx, pool, rdb)
	log.Println("Delivery worker started.")

	for {
		results, err := rdb.XReadGroup(ctx, &goredis.XReadGroupArgs{
			Group:    deliveryGroup,
			Consumer: deliveryConsumer,
			Streams:  []string{deliveriesStream, ">"},
			Count:    10,
			Block:    5 * time.Second,
		}).Result()
		if err != nil && err != goredis.Nil {
			log.Printf("Delivery worker error: %v", err)
			time.Sleep(time.Second)
			continue
		}
		for _, stream := range results {
			for _, msg := range stream.Messages {
				processOne(ctx, pool, rdb, msg.ID, msg.Values)
			}
		}
	}
}
