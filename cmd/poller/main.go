package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	goredis "github.com/redis/go-redis/v9"
)

const (
	transitionsStream = "events:transitions"
	gameStateTTL      = 86400 // 24 hours
)

var nonLiveStates = map[string]bool{
	"Warmup":           true,
	"Pre-Game":         true,
	"Delayed Start":    true,
	"Scheduled":        true,
	"Final":            true,
	"Game Over":        true,
	"Completed":        true,
	"Completed Early":  true,
	"Postponed":        true,
	"Cancelled":        true,
	"Suspended":        true,
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

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
	case "run-poller":
		runPoller()
	case "poll-once":
		pollOnce()
	case "parse-diff-patch":
		parseDiffPatchCmd(args[1:])
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
	fmt.Println("  run-poller        Run the polling loop")
	fmt.Println("  poll-once         Run exactly one poll cycle and exit")
	fmt.Println("  parse-diff-patch  Parse a diffPatch response and print JSON result")
}

// ---------------------------------------------------------------------------
// parse-diff-patch command
// ---------------------------------------------------------------------------

func parseDiffPatchCmd(args []string) {
	if len(args) < 1 {
		fmt.Fprintln(os.Stderr, "usage: parse-diff-patch <patch_json_path> --start-timecode <ts>")
		os.Exit(1)
	}
	filePath := args[0]
	startTimecode := ""
	for i := 1; i < len(args); i++ {
		if args[i] == "--start-timecode" && i+1 < len(args) {
			startTimecode = args[i+1]
			i++
		}
	}

	data, err := os.ReadFile(filePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error reading file: %v\n", err)
		os.Exit(1)
	}

	var body any
	if err := json.Unmarshal(data, &body); err != nil {
		fmt.Fprintf(os.Stderr, "error parsing JSON: %v\n", err)
		os.Exit(1)
	}

	fullResp, newTimecode, needsFullFetch := parseDiffPatchBody(body, startTimecode)

	result := map[string]any{
		"full_response":    fullResp,
		"new_timecode":     newTimecode,
		"needs_full_fetch": needsFullFetch,
	}

	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(result); err != nil {
		fmt.Fprintf(os.Stderr, "error encoding output: %v\n", err)
		os.Exit(1)
	}
}

// parseDiffPatchBody parses a diffPatch API response.
// Returns (fullResponse, newTimecode, needsFullFetch).
// fullResponse is non-nil only for a full-update dict response.
// needsFullFetch is true if an offense op was detected and the caller must fetch the full feed.
func parseDiffPatchBody(body any, startTimecode string) (map[string]any, string, bool) {
	switch v := body.(type) {
	case []any:
		// Flatten all ops from each envelope's "diff" array
		var allOps []map[string]any
		for _, item := range v {
			if m, ok := item.(map[string]any); ok {
				if diffList, ok := m["diff"].([]any); ok {
					for _, op := range diffList {
						if opMap, ok := op.(map[string]any); ok {
							allOps = append(allOps, opMap)
						}
					}
				}
			}
		}

		for _, op := range allOps {
			if path, ok := op["path"].(string); ok && strings.Contains(path, "offense") {
				return nil, startTimecode, true
			}
		}

		newTS := startTimecode
		for _, op := range allOps {
			if path, ok := op["path"].(string); ok && path == "/metaData/timeStamp" {
				if val, ok := op["value"].(string); ok {
					newTS = val
					break
				}
			}
		}
		return nil, newTS, false

	case map[string]any:
		newTS := startTimecode
		if meta, ok := v["metaData"].(map[string]any); ok {
			if ts, ok := meta["timeStamp"].(string); ok {
				newTS = ts
			}
		}
		return v, newTS, false
	}

	return nil, startTimecode, false
}

// ---------------------------------------------------------------------------
// Poller commands
// ---------------------------------------------------------------------------

func connectRedis() *goredis.Client {
	redisURL := os.Getenv("REDIS_URL")
	opts, err := goredis.ParseURL(redisURL)
	if err != nil {
		log.Fatalf("redis url: %v", err)
	}
	return goredis.NewClient(opts)
}

func mlbBaseURL() string {
	if b := os.Getenv("MLB_API_BASE_URL"); b != "" {
		return b
	}
	return "https://ws.statsapi.mlb.com"
}

func pollOnce() {
	ctx := context.Background()
	rdb := connectRedis()
	defer rdb.Close()

	timecodes := make(map[int]string)
	if err := pollIteration(ctx, mlbBaseURL(), rdb, timecodes); err != nil {
		log.Fatalf("poll-once: %v", err)
	}
}

func runPoller() {
	ctx := context.Background()
	rdb := connectRedis()
	defer rdb.Close()

	interval := 10
	if s := os.Getenv("POLL_INTERVAL_SECONDS"); s != "" {
		if n, err := strconv.Atoi(s); err == nil {
			interval = n
		}
	}

	timecodes := make(map[int]string)
	log.Printf("Poller started. Polling every %ds.", interval)
	for {
		if err := pollIteration(ctx, mlbBaseURL(), rdb, timecodes); err != nil {
			log.Printf("Poller error: %v", err)
		}
		time.Sleep(time.Duration(interval) * time.Second)
	}
}

// ---------------------------------------------------------------------------
// Poll iteration
// ---------------------------------------------------------------------------

type GameInfo struct {
	GamePK       int
	HomeTeamID   int
	HomeTeamName string
	AwayTeamID   int
	AwayTeamName string
	Status       string
}

var mlbHTTPClient = &http.Client{Timeout: 15 * time.Second}

func pollIteration(ctx context.Context, baseURL string, rdb *goredis.Client, timecodes map[int]string) error {
	games, err := getLiveGames(ctx, baseURL)
	if err != nil {
		return fmt.Errorf("get live games: %w", err)
	}
	if len(games) == 0 {
		fmt.Println("Poller: no live games.")
		return nil
	}

	for _, game := range games {
		var liveData map[string]any

		if _, seen := timecodes[game.GamePK]; !seen {
			// First poll: fetch full live feed
			liveData, err = getLiveFeed(ctx, baseURL, game.GamePK)
			if err != nil {
				log.Printf("Poller error for game %d: %v", game.GamePK, err)
				continue
			}
			if meta, ok := liveData["metaData"].(map[string]any); ok {
				if ts, ok := meta["timeStamp"].(string); ok {
					timecodes[game.GamePK] = ts
				} else {
					timecodes[game.GamePK] = ""
				}
			} else {
				timecodes[game.GamePK] = ""
			}
		} else {
			// Subsequent polls: fetch diffPatch
			var newTS string
			liveData, newTS, err = getDiffPatch(ctx, baseURL, game.GamePK, timecodes[game.GamePK])
			if err != nil {
				log.Printf("Poller error for game %d: %v", game.GamePK, err)
				continue
			}
			timecodes[game.GamePK] = newTS
			if liveData == nil {
				continue // no offense change, nothing to process
			}
		}

		n, err := processGame(ctx, rdb, game.GamePK, liveData, game)
		if err != nil {
			log.Printf("Poller error processing game %d: %v", game.GamePK, err)
			continue
		}
		if n > 0 {
			log.Printf("Poller: game %d emitted %d transition(s).", game.GamePK, n)
		}
	}
	return nil
}

// ---------------------------------------------------------------------------
// MLB API client
// ---------------------------------------------------------------------------

func getSchedule(ctx context.Context, baseURL string, gameDate string) ([]GameInfo, error) {
	reqURL := baseURL + "/api/v1/schedule"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, err
	}
	q := req.URL.Query()
	q.Set("sportId", "1")
	q.Set("date", gameDate)
	q.Set("hydrate", "team,linescore")
	req.URL.RawQuery = q.Encode()

	resp, err := mlbHTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, err
	}

	var games []GameInfo
	for _, dateEntry := range sliceOf(data["dates"]) {
		de, ok := dateEntry.(map[string]any)
		if !ok {
			continue
		}
		for _, g := range sliceOf(de["games"]) {
			gm, ok := g.(map[string]any)
			if !ok {
				continue
			}
			status, _ := gm["status"].(map[string]any)
			abstractState, _ := status["abstractGameState"].(string)
			teams, _ := gm["teams"].(map[string]any)
			home, _ := teams["home"].(map[string]any)
			away, _ := teams["away"].(map[string]any)
			homeTeam, _ := home["team"].(map[string]any)
			awayTeam, _ := away["team"].(map[string]any)

			gamePKf, _ := gm["gamePk"].(float64)
			homeID, _ := homeTeam["id"].(float64)
			awayID, _ := awayTeam["id"].(float64)

			games = append(games, GameInfo{
				GamePK:       int(gamePKf),
				HomeTeamID:   int(homeID),
				HomeTeamName: str(homeTeam["name"]),
				AwayTeamID:   int(awayID),
				AwayTeamName: str(awayTeam["name"]),
				Status:       abstractState,
			})
		}
	}
	return games, nil
}

func getLiveGames(ctx context.Context, baseURL string) ([]GameInfo, error) {
	et, err := time.LoadLocation("America/New_York")
	if err != nil {
		// fallback: use UTC if tzdata unavailable
		et = time.UTC
	}
	nowET := time.Now().In(et)
	gameDate := nowET.Format("01/02/2006")

	games, err := getSchedule(ctx, baseURL, gameDate)
	if err != nil {
		return nil, err
	}

	var live []GameInfo
	for _, g := range games {
		if g.Status == "Live" {
			live = append(live, g)
		}
	}

	if len(live) == 0 && nowET.Hour() < 6 {
		yesterday := nowET.Add(-24 * time.Hour).Format("01/02/2006")
		yday, err := getSchedule(ctx, baseURL, yesterday)
		if err != nil {
			return nil, err
		}
		for _, g := range yday {
			if g.Status == "Live" {
				live = append(live, g)
			}
		}
	}

	return live, nil
}

func getLiveFeed(ctx context.Context, baseURL string, gamePK int) (map[string]any, error) {
	reqURL := fmt.Sprintf("%s/api/v1.1/game/%d/feed/live", baseURL, gamePK)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, err
	}
	resp, err := mlbHTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, err
	}
	return data, nil
}

// getDiffPatch fetches the diffPatch endpoint and returns (liveData, newTimecode, error).
// liveData is nil when the patch contains no offense changes (caller should skip process_game).
// If the patch indicates offense changes, a full live feed is fetched automatically.
// newTimecode equals startTimecode if a full fetch was triggered (timecode unchanged).
func getDiffPatch(ctx context.Context, baseURL string, gamePK int, startTimecode string) (map[string]any, string, error) {
	reqURL := fmt.Sprintf("%s/api/v1.1/game/%d/feed/live/diffPatch", baseURL, gamePK)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, startTimecode, err
	}
	q := req.URL.Query()
	q.Set("startTimecode", startTimecode)
	req.URL.RawQuery = q.Encode()

	resp, err := mlbHTTPClient.Do(req)
	if err != nil {
		return nil, startTimecode, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, startTimecode, err
	}

	var rawBody any
	if err := json.Unmarshal(body, &rawBody); err != nil {
		return nil, startTimecode, err
	}

	fullResp, newTS, needsFull := parseDiffPatchBody(rawBody, startTimecode)
	if needsFull {
		full, err := getLiveFeed(ctx, baseURL, gamePK)
		return full, startTimecode, err
	}
	return fullResp, newTS, nil
}

// ---------------------------------------------------------------------------
// Diff engine
// ---------------------------------------------------------------------------

func processGame(ctx context.Context, rdb *goredis.Client, gamePK int, liveData map[string]any, game GameInfo) (int, error) {
	if !isGameInProgress(liveData) {
		return 0, nil
	}

	offense := extractOffenseState(liveData)
	if len(offense) == 0 {
		return 0, nil
	}

	inning, inningHalf, outs := extractInningState(liveData)
	stateKey := fmt.Sprintf("game:%d:offense", gamePK)

	prev, err := rdb.HGetAll(ctx, stateKey).Result()
	if err != nil {
		return 0, fmt.Errorf("hgetall %s: %w", stateKey, err)
	}

	type position struct {
		posKey      string
		streamState string
		player      map[string]any
	}

	positions := []position{
		{"batter", "at_bat", mapOf(offense["batter"])},
		{"onDeck", "on_deck", mapOf(offense["onDeck"])},
	}

	eventsEmitted := 0
	newState := make(map[string]string)

	for _, pos := range positions {
		if pos.player == nil {
			continue
		}
		playerIDf, _ := pos.player["id"].(float64)
		playerID := strconv.Itoa(int(playerIDf))
		playerName := str(pos.player["fullName"])
		if playerID == "" || playerID == "0" {
			continue
		}
		newState[pos.posKey] = playerID

		if playerID != prev[pos.posKey] {
			eventID := newUUID()
			fields := map[string]any{
				"event_id":       eventID,
				"game_id":        strconv.Itoa(gamePK),
				"player_id":      playerID,
				"player_name":    playerName,
				"state":          pos.streamState,
				"home_team_id":   strconv.Itoa(game.HomeTeamID),
				"home_team_name": game.HomeTeamName,
				"away_team_id":   strconv.Itoa(game.AwayTeamID),
				"away_team_name": game.AwayTeamName,
				"inning":         strconv.Itoa(inning),
				"inning_half":    inningHalf,
				"outs":           strconv.Itoa(outs),
				"occurred_at":    nowISO(),
			}
			if err := rdb.XAdd(ctx, &goredis.XAddArgs{
				Stream: transitionsStream,
				Values: fields,
			}).Err(); err != nil {
				return eventsEmitted, fmt.Errorf("xadd transitions: %w", err)
			}
			eventsEmitted++
		}
	}

	if len(newState) > 0 {
		mapping := make(map[string]any, len(newState))
		for k, v := range newState {
			mapping[k] = v
		}
		if err := rdb.HSet(ctx, stateKey, mapping).Err(); err != nil {
			return eventsEmitted, fmt.Errorf("hset %s: %w", stateKey, err)
		}
		if err := rdb.Expire(ctx, stateKey, gameStateTTL*time.Second).Err(); err != nil {
			return eventsEmitted, fmt.Errorf("expire %s: %w", stateKey, err)
		}
	}

	return eventsEmitted, nil
}

func isGameInProgress(liveData map[string]any) bool {
	gameData, ok := liveData["gameData"].(map[string]any)
	if !ok {
		return false
	}
	status, ok := gameData["status"].(map[string]any)
	if !ok {
		return false
	}
	detailed, _ := status["detailedState"].(string)
	return !nonLiveStates[detailed]
}

func extractOffenseState(liveData map[string]any) map[string]any {
	ld, ok := liveData["liveData"].(map[string]any)
	if !ok {
		return nil
	}
	ls, ok := ld["linescore"].(map[string]any)
	if !ok {
		return nil
	}
	offense, _ := ls["offense"].(map[string]any)
	return offense
}

func extractInningState(liveData map[string]any) (int, string, int) {
	ld, ok := liveData["liveData"].(map[string]any)
	if !ok {
		return 0, "", 0
	}
	ls, ok := ld["linescore"].(map[string]any)
	if !ok {
		return 0, "", 0
	}

	inningf, _ := ls["currentInning"].(float64)
	inning := int(inningf)
	isTop, _ := ls["isTopInning"].(bool)
	outsf, _ := ls["outs"].(float64)
	outs := int(outsf)

	if outs == 3 {
		if isTop {
			return inning, "Bot", 0
		}
		return inning + 1, "Top", 0
	}

	half := "Bot"
	if isTop {
		half = "Top"
	}
	return inning, half, outs
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func nowISO() string {
	if fixed := os.Getenv("ATBATWATCH_FIXED_NOW"); fixed != "" {
		return fixed
	}
	return time.Now().UTC().Format("2006-01-02T15:04:05.000000") + "+00:00"
}

func newUUID() string {
	b := make([]byte, 16)
	rand.Read(b)
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func str(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

func sliceOf(v any) []any {
	if s, ok := v.([]any); ok {
		return s
	}
	return nil
}

func mapOf(v any) map[string]any {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return nil
}

