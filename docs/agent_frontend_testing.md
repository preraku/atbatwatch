# Agent frontend testing

How the agent should verify frontend changes against the running local stack.

## 1. Ensure Colima is running

```bash
docker info > /dev/null 2>&1 || colima start
```

## 2. Ensure .env exists

```bash
[ -f .env ] || cp .env.example .env
```

## 3. Bring up Docker services

```bash
docker compose -f docker-compose.prod.yml up -d --build postgres redis migrate poller fanout delivery api
```

Wait for `migrate` to exit cleanly:

```bash
for i in $(seq 1 12); do
  status=$(docker compose -f docker-compose.prod.yml ps migrate --format json 2>/dev/null \
           | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State'])" 2>/dev/null)
  [ "$status" = "exited" ] && break
  sleep 5
done
```

Sanity-check the API:

```bash
curl -sf http://localhost:8000/docs > /dev/null && echo "API OK"
```

## 4. Start the frontend HTTP server

The frontend uses ES modules and must be served over HTTP, not `file://`.

```bash
lsof -ti :8080 | xargs kill -9 2>/dev/null || true
cd frontend && uv run -m http.server 8080 &
FRONTEND_PID=$!
cd ..
for i in $(seq 1 10); do curl -s http://localhost:8080 > /dev/null && break; sleep 1; done
```

After testing, shut it down:

```bash
kill $FRONTEND_PID 2>/dev/null || lsof -ti :8080 | xargs kill -9 2>/dev/null || true
```

## 5. Test with playwright-cli

Use a named session so commands share one browser tab. Run `playwright-cli --help` for command reference.

Typical flow: open → snapshot to inspect → fill/click to interact → snapshot to verify.

Test accounts created during a run persist in Postgres until `down -v`.

## 6. Tear down

```bash
docker compose -f docker-compose.prod.yml down      # keeps volumes
docker compose -f docker-compose.prod.yml down -v   # full wipe
```
