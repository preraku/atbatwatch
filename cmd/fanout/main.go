package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	goredis "github.com/redis/go-redis/v9"
)

const (
	transitionsStream = "events:transitions"
	deliveriesStream  = "events:deliveries"
	fanoutGroup       = "fanout-group"
	fanoutConsumer    = "fanout-1"
)

func main() {
	args := os.Args[1:]
	// When run via "docker run <image> atbatwatch <cmd>", ENTRYPOINT is already
	// "atbatwatch", so the container receives ["atbatwatch", "atbatwatch", "<cmd>"].
	// Strip the extra "atbatwatch" prefix if present.
	if len(args) > 0 && args[0] == "atbatwatch" {
		args = args[1:]
	}
	if len(args) == 0 {
		printHelp()
		os.Exit(0)
	}
	switch args[0] {
	case "run-fanout":
		if err := runFanout(); err != nil {
			log.Fatalf("run-fanout: %v", err)
		}
	case "fanout-once":
		if err := fanoutOnce(); err != nil {
			log.Fatalf("fanout-once: %v", err)
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
	fmt.Println("  run-fanout   Run the fanout worker (loop)")
	fmt.Println("  fanout-once  Process all pending transitions once and exit")
}

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
	rdb.XGroupCreateMkStream(ctx, transitionsStream, fanoutGroup, "0")
}

// reclaimPEL reclaims messages idle for >30s from a previous crashed consumer.
func reclaimPEL(ctx context.Context, pool *pgxpool.Pool, rdb *goredis.Client) {
	cursor := "0-0"
	for {
		msgs, next, err := rdb.XAutoClaim(ctx, &goredis.XAutoClaimArgs{
			Stream:   transitionsStream,
			Group:    fanoutGroup,
			Consumer: fanoutConsumer,
			MinIdle:  30 * time.Second,
			Start:    cursor,
			Count:    100,
		}).Result()
		if err != nil {
			log.Printf("fanout: xautoclaim: %v", err)
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

type follower struct {
	userID    int64
	webhookURL string
}

func getFollowers(ctx context.Context, pool *pgxpool.Pool, playerID int64) ([]follower, error) {
	rows, err := pool.Query(ctx,
		`SELECT u.user_id, u.notification_target_id
		 FROM users u
		 JOIN follows f ON u.user_id = f.user_id
		 WHERE f.player_id = $1`,
		playerID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var followers []follower
	for rows.Next() {
		var f follower
		if err := rows.Scan(&f.userID, &f.webhookURL); err != nil {
			return nil, err
		}
		followers = append(followers, f)
	}
	return followers, rows.Err()
}

func processOne(ctx context.Context, pool *pgxpool.Pool, rdb *goredis.Client, msgID string, fields map[string]interface{}) {
	playerIDStr, _ := fields["player_id"].(string)
	playerID, err := strconv.ParseInt(playerIDStr, 10, 64)
	if err != nil {
		log.Printf("fanout: invalid player_id %q: %v", playerIDStr, err)
		return
	}

	followers, err := getFollowers(ctx, pool, playerID)
	if err != nil {
		log.Printf("fanout: get followers for player %s: %v", playerIDStr, err)
		return
	}

	anyFailed := false
	for _, f := range followers {
		delivery := make(map[string]interface{}, len(fields)+2)
		for k, v := range fields {
			delivery[k] = v
		}
		delivery["user_id"] = fmt.Sprintf("%d", f.userID)
		delivery["webhook_url"] = f.webhookURL

		if err := rdb.XAdd(ctx, &goredis.XAddArgs{
			Stream: deliveriesStream,
			Values: delivery,
		}).Err(); err != nil {
			log.Printf("fanout: xadd delivery for user %d: %v", f.userID, err)
			anyFailed = true
			continue
		}
	}

	if !anyFailed {
		rdb.XAck(ctx, transitionsStream, fanoutGroup, msgID)
	}
}

func fanoutOnce() error {
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
		Group:    fanoutGroup,
		Consumer: fanoutConsumer,
		Streams:  []string{transitionsStream, ">"},
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

func runFanout() error {
	ctx := context.Background()
	pool, rdb, err := connect(ctx)
	if err != nil {
		return err
	}
	defer pool.Close()
	defer rdb.Close()

	ensureGroup(ctx, rdb)
	reclaimPEL(ctx, pool, rdb)
	log.Println("Fanout worker started.")

	for {
		results, err := rdb.XReadGroup(ctx, &goredis.XReadGroupArgs{
			Group:    fanoutGroup,
			Consumer: fanoutConsumer,
			Streams:  []string{transitionsStream, ">"},
			Count:    10,
			Block:    5 * time.Second,
		}).Result()
		if err != nil && err != goredis.Nil {
			log.Printf("Fanout worker error: %v", err)
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
