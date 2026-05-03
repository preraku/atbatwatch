package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	jwtlib "github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/argon2"
)

const (
	jwtAlgorithm    = "HS256"
	tokenExpireDays = 30
	argon2Memory    = 65536
	argon2Iters     = 2
	argon2Threads   = 4
	argon2KeyLen    = 32
	argon2SaltLen   = 16
)

var (
	jwtSecret    []byte
	dbPool       *pgxpool.Pool
	mlbBase      string
	corsOrigins  []string
)

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
	case "run-api":
		if err := runAPI(); err != nil {
			log.Fatalf("run-api: %v", err)
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
	fmt.Println("  run-api  Run the HTTP API server")
}

func runAPI() error {
	jwtSecret = []byte(mustEnv("JWT_SECRET"))
	mlbBase = os.Getenv("MLB_API_BASE_URL")
	if mlbBase == "" {
		mlbBase = "https://ws.statsapi.mlb.com"
	}
	if raw := os.Getenv("CORS_ORIGIN"); raw != "" {
		for _, o := range strings.Split(raw, ",") {
			if o = strings.TrimSpace(o); o != "" {
				corsOrigins = append(corsOrigins, o)
			}
		}
	}

	ctx := context.Background()
	var err error
	dbPool, err = pgxpool.New(ctx, pgxDSN(mustEnv("DATABASE_URL")))
	if err != nil {
		return fmt.Errorf("postgres: %w", err)
	}
	defer dbPool.Close()

	mux := http.NewServeMux()

	// Health check — acceptance conftest polls /docs
	mux.HandleFunc("GET /docs", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"ok"}`))
	})

	// Auth
	mux.HandleFunc("POST /auth/signup", handleSignup)
	mux.HandleFunc("POST /auth/login", handleLogin)

	// Follows (auth required)
	mux.HandleFunc("GET /me/follows", authMiddleware(handleListFollows))
	mux.HandleFunc("POST /me/follows", authMiddleware(handleAddFollow))
	mux.HandleFunc("DELETE /me/follows/{player_id}", authMiddleware(handleDeleteFollow))

	// Player search (auth required)
	mux.HandleFunc("GET /players/search", authMiddleware(handlePlayerSearch))

	port := os.Getenv("PORT")
	if port == "" {
		port = "8000"
	}
	log.Printf("API server starting on :%s", port)
	srv := &http.Server{
		Addr:         ":" + port,
		Handler:      corsMiddleware(mux),
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  120 * time.Second,
	}
	return srv.ListenAndServe()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		for _, allowed := range corsOrigins {
			if origin == allowed {
				w.Header().Set("Access-Control-Allow-Origin", origin)
				w.Header().Set("Access-Control-Allow-Credentials", "true")
				w.Header().Set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
				w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type")
				break
			}
		}
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		log.Fatalf("required env var %s is not set", key)
	}
	return v
}

func pgxDSN(raw string) string {
	return strings.Replace(raw, "postgresql+asyncpg://", "postgresql://", 1)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]string{"detail": detail})
}

// ---------------------------------------------------------------------------
// Argon2id password hashing (MCF format)
// ---------------------------------------------------------------------------

func hashPassword(password string) (string, error) {
	salt := make([]byte, argon2SaltLen)
	if _, err := rand.Read(salt); err != nil {
		return "", err
	}
	hash := argon2.IDKey([]byte(password), salt, argon2Iters, argon2Memory, argon2Threads, argon2KeyLen)
	enc := base64.RawStdEncoding
	return fmt.Sprintf("$argon2id$v=19$m=%d,t=%d,p=%d$%s$%s",
		argon2Memory, argon2Iters, argon2Threads,
		enc.EncodeToString(salt),
		enc.EncodeToString(hash),
	), nil
}

func verifyPassword(password, encoded string) bool {
	parts := strings.Split(encoded, "$")
	// $argon2id$v=19$m=...,t=...,p=...$<salt>$<hash>
	if len(parts) != 6 || parts[1] != "argon2id" {
		return false
	}
	var m, t, p uint32
	_, err := fmt.Sscanf(parts[3], "m=%d,t=%d,p=%d", &m, &t, &p)
	if err != nil || p > 255 {
		return false
	}
	enc := base64.RawStdEncoding
	salt, err := enc.DecodeString(parts[4])
	if err != nil {
		return false
	}
	expectedHash, err := enc.DecodeString(parts[5])
	if err != nil {
		return false
	}
	keyLen := uint32(len(expectedHash))
	hash := argon2.IDKey([]byte(password), salt, t, m, uint8(p), keyLen)
	if len(hash) != len(expectedHash) {
		return false
	}
	// constant-time comparison
	var diff byte
	for i := range hash {
		diff |= hash[i] ^ expectedHash[i]
	}
	return diff == 0
}

// ---------------------------------------------------------------------------
// JWT
// ---------------------------------------------------------------------------

func makeToken(userID int64) (string, error) {
	exp := time.Now().UTC().Add(tokenExpireDays * 24 * time.Hour)
	claims := jwtlib.MapClaims{
		"sub": strconv.FormatInt(userID, 10),
		"exp": exp.Unix(),
	}
	tok := jwtlib.NewWithClaims(jwtlib.SigningMethodHS256, claims)
	return tok.SignedString(jwtSecret)
}

func parseToken(tokenStr string) (int64, error) {
	tok, err := jwtlib.Parse(tokenStr, func(t *jwtlib.Token) (any, error) {
		if _, ok := t.Method.(*jwtlib.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
		}
		return jwtSecret, nil
	})
	if err != nil || !tok.Valid {
		return 0, fmt.Errorf("invalid token")
	}
	claims, ok := tok.Claims.(jwtlib.MapClaims)
	if !ok {
		return 0, fmt.Errorf("bad claims")
	}
	sub, err := claims.GetSubject()
	if err != nil {
		return 0, err
	}
	return strconv.ParseInt(sub, 10, 64)
}

// ---------------------------------------------------------------------------
// Auth middleware
// ---------------------------------------------------------------------------

type userIDKey struct{}

func authMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		authHeader := r.Header.Get("Authorization")
		if !strings.HasPrefix(authHeader, "Bearer ") {
			writeError(w, http.StatusUnauthorized, "Missing bearer token")
			return
		}
		tokenStr := strings.TrimPrefix(authHeader, "Bearer ")
		userID, err := parseToken(tokenStr)
		if err != nil {
			writeError(w, http.StatusUnauthorized, "Invalid token")
			return
		}
		ctx := context.WithValue(r.Context(), userIDKey{}, userID)
		next(w, r.WithContext(ctx))
	}
}

func currentUserID(r *http.Request) int64 {
	return r.Context().Value(userIDKey{}).(int64)
}

// ---------------------------------------------------------------------------
// Auth handlers
// ---------------------------------------------------------------------------

type signupRequest struct {
	Email          string `json:"email"`
	Password       string `json:"password"`
	DiscordWebhook string `json:"discord_webhook"`
}

func handleSignup(w http.ResponseWriter, r *http.Request) {
	var req signupRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "Invalid JSON")
		return
	}
	if req.Email == "" || req.Password == "" || req.DiscordWebhook == "" {
		writeError(w, http.StatusUnprocessableEntity, "email, password, and discord_webhook are required")
		return
	}

	ctx := r.Context()

	hash, err := hashPassword(req.Password)
	if err != nil {
		log.Printf("hash password: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal error")
		return
	}

	var userID int64
	err = dbPool.QueryRow(ctx,
		`INSERT INTO users (email, password_hash, notification_target_type, notification_target_id)
		 VALUES ($1, $2, 'discord', $3) RETURNING user_id`,
		req.Email, hash, req.DiscordWebhook,
	).Scan(&userID)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			writeError(w, http.StatusConflict, "Email already registered")
			return
		}
		log.Printf("signup insert: %v", err)
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	token, err := makeToken(userID)
	if err != nil {
		log.Printf("make token: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal error")
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"token": token})
}

type loginRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

func handleLogin(w http.ResponseWriter, r *http.Request) {
	var req loginRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "Invalid JSON")
		return
	}

	ctx := r.Context()

	var userID int64
	var hash string
	err := dbPool.QueryRow(ctx,
		"SELECT user_id, password_hash FROM users WHERE email=$1", req.Email,
	).Scan(&userID, &hash)
	if err == pgx.ErrNoRows || (err == nil && !verifyPassword(req.Password, hash)) {
		writeError(w, http.StatusUnauthorized, "Invalid credentials")
		return
	}
	if err != nil {
		log.Printf("login query: %v", err)
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	token, err := makeToken(userID)
	if err != nil {
		log.Printf("make token: %v", err)
		writeError(w, http.StatusInternalServerError, "Internal error")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"token": token})
}

// ---------------------------------------------------------------------------
// Follows handlers
// ---------------------------------------------------------------------------

type playerRow struct {
	PlayerID int64   `json:"player_id"`
	FullName string  `json:"full_name"`
	Team     *string `json:"team"`
	Position *string `json:"position"`
}

func handleListFollows(w http.ResponseWriter, r *http.Request) {
	userID := currentUserID(r)
	ctx := r.Context()

	rows, err := dbPool.Query(ctx,
		`SELECT p.player_id, p.full_name, p.team, p.position
		 FROM players p
		 JOIN follows f ON p.player_id = f.player_id
		 WHERE f.user_id = $1
		 ORDER BY p.full_name`,
		userID,
	)
	if err != nil {
		log.Printf("list follows: %v", err)
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	defer rows.Close()

	var follows []playerRow
	for rows.Next() {
		var p playerRow
		if err := rows.Scan(&p.PlayerID, &p.FullName, &p.Team, &p.Position); err != nil {
			log.Printf("list follows scan: %v", err)
			writeError(w, http.StatusInternalServerError, "Database error")
			return
		}
		follows = append(follows, p)
	}
	if err := rows.Err(); err != nil {
		log.Printf("list follows: %v", err)
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	if follows == nil {
		follows = []playerRow{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"follows": follows})
}

type followRequest struct {
	PlayerID int64   `json:"player_id"`
	FullName string  `json:"full_name"`
	Team     *string `json:"team"`
	Position *string `json:"position"`
}

func handleAddFollow(w http.ResponseWriter, r *http.Request) {
	var req followRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "Invalid JSON")
		return
	}
	if req.PlayerID == 0 || req.FullName == "" {
		writeError(w, http.StatusUnprocessableEntity, "player_id and full_name are required")
		return
	}

	userID := currentUserID(r)
	ctx := r.Context()

	// Upsert player
	_, err := dbPool.Exec(ctx,
		`INSERT INTO players (player_id, full_name, team, position)
		 VALUES ($1, $2, $3, $4)
		 ON CONFLICT (player_id) DO UPDATE
		   SET full_name = EXCLUDED.full_name,
		       team = COALESCE(EXCLUDED.team, players.team),
		       position = COALESCE(EXCLUDED.position, players.position)`,
		req.PlayerID, req.FullName, req.Team, req.Position,
	)
	if err != nil {
		log.Printf("upsert player: %v", err)
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	// Add follow (idempotent)
	_, err = dbPool.Exec(ctx,
		`INSERT INTO follows (user_id, player_id) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
		userID, req.PlayerID,
	)
	if err != nil {
		log.Printf("add follow: %v", err)
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}

	writeJSON(w, http.StatusCreated, map[string]any{
		"player_id": req.PlayerID,
		"full_name": req.FullName,
		"team":      req.Team,
		"position":  req.Position,
	})
}

func handleDeleteFollow(w http.ResponseWriter, r *http.Request) {
	playerIDStr := r.PathValue("player_id")
	playerID, err := strconv.ParseInt(playerIDStr, 10, 64)
	if err != nil {
		writeError(w, http.StatusNotFound, "Follow not found")
		return
	}

	userID := currentUserID(r)
	ctx := r.Context()

	result, err := dbPool.Exec(ctx,
		"DELETE FROM follows WHERE user_id=$1 AND player_id=$2",
		userID, playerID,
	)
	if err != nil {
		log.Printf("delete follow: %v", err)
		writeError(w, http.StatusInternalServerError, "Database error")
		return
	}
	if result.RowsAffected() == 0 {
		writeError(w, http.StatusNotFound, "Follow not found")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// ---------------------------------------------------------------------------
// Player search handler
// ---------------------------------------------------------------------------

func handlePlayerSearch(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query().Get("q")
	if len(q) < 2 {
		writeJSON(w, http.StatusOK, map[string]any{"players": []any{}})
		return
	}

	client := &http.Client{Timeout: 15 * time.Second}

	// Search
	searchURL := mlbBase + "/api/v1/people/search"
	searchReq, _ := http.NewRequestWithContext(r.Context(), http.MethodGet, searchURL, nil)
	qp := searchReq.URL.Query()
	qp.Set("names", q)
	searchReq.URL.RawQuery = qp.Encode()

	searchResp, err := client.Do(searchReq)
	if err != nil {
		log.Printf("player search: %v", err)
		writeJSON(w, http.StatusOK, map[string]any{"players": []any{}})
		return
	}
	defer searchResp.Body.Close()

	body, _ := io.ReadAll(searchResp.Body)
	var searchData map[string]any
	if err := json.Unmarshal(body, &searchData); err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"players": []any{}})
		return
	}

	rawPeople, _ := searchData["people"].([]any)
	var active []map[string]any
	for _, p := range rawPeople {
		person, ok := p.(map[string]any)
		if !ok {
			continue
		}
		if act, ok := person["active"].(bool); ok && !act {
			continue
		}
		active = append(active, person)
	}

	if len(active) == 0 {
		writeJSON(w, http.StatusOK, map[string]any{"players": []any{}})
		return
	}

	type personDetail struct {
		idx    int
		detail map[string]any
		err    error
	}

	results := make([]map[string]any, len(active))
	detailCh := make(chan personDetail, len(active))

	for i, p := range active {
		go func(idx int, pid any) {
			idStr := fmt.Sprintf("%v", pid)
			url := fmt.Sprintf("%s/api/v1/people/%s?hydrate=currentTeam", mlbBase, idStr)
			req2, _ := http.NewRequestWithContext(r.Context(), http.MethodGet, url, nil)
			resp2, err := client.Do(req2)
			if err != nil {
				detailCh <- personDetail{idx: idx, err: err}
				return
			}
			defer resp2.Body.Close()
			b2, _ := io.ReadAll(resp2.Body)
			var d2 map[string]any
			json.Unmarshal(b2, &d2)
			people2, _ := d2["people"].([]any)
			if len(people2) == 0 {
				detailCh <- personDetail{idx: idx, err: fmt.Errorf("not found")}
				return
			}
			detail, _ := people2[0].(map[string]any)
			detailCh <- personDetail{idx: idx, detail: detail}
		}(i, p["id"])
	}

	for range active {
		pd := <-detailCh
		if pd.err != nil || pd.detail == nil {
			continue
		}
		results[pd.idx] = pd.detail
	}

	var players []map[string]any
	for i, p := range active {
		detail := results[i]
		if detail == nil {
			continue
		}
		team, _ := detail["currentTeam"].(map[string]any)
		if _, hasParent := team["parentOrgId"]; hasParent {
			continue
		}
		var teamName *string
		if name, ok := team["name"].(string); ok {
			teamName = &name
		}
		var position *string
		if pos, ok := detail["primaryPosition"].(map[string]any); ok {
			if abbr, ok := pos["abbreviation"].(string); ok {
				position = &abbr
			}
		}
		playerID := int64(0)
		switch v := p["id"].(type) {
		case float64:
			playerID = int64(v)
		case int64:
			playerID = v
		}
		players = append(players, map[string]any{
			"player_id": playerID,
			"full_name": p["fullName"],
			"team":      teamName,
			"position":  position,
		})
	}
	if players == nil {
		players = []map[string]any{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"players": players})
}
