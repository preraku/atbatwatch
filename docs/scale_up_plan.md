# At Bat Watch — System Design

## Overview

At Bat Watch notifies users when an MLB player they follow is at bat or on deck. Users register, select players to follow, and receive push notifications on state transitions during live games. This document specifies the architecture, data model, and scaling approach from MVP through ~1M users.

## Workload characterization

"At bat" and "on deck" are states, not events. An at-bat lasts roughly 3–5 minutes. Across 15 simultaneous games, the system produces approximately 3–10 state transitions per second at peak — not one notification per polling interval per player. Notifications must be emitted only on transitions.

Fan-out per transition is bounded by followers of the transitioning player. At 1M users, most transitions fan out to a small fraction of the base, but a popular player (Ohtani, Judge) could fan out to hundreds of thousands simultaneously. This is the primary scaling axis: bursty fan-out, not sustained throughput.

Polling frequency: every 10 seconds against MLB's StatsAPI. Use the diff/timestamp endpoint where available to fetch only changed state. MLB's unofficial API has no SLA; cache aggressively and back off on errors.

## Data model (Postgres)

Three primary tables plus a delivery log:

**users**
- `user_id` (PK)
- `email`
- `password_hash` (argon2id)
- `notification_target_id` (Discord webhook URL in the MVP; will need a `notification_target_type` discriminator when FCM/APNs are added)
- `created_at`

**players**
- `player_id` (PK)
- `mlb_id` (unique)
- `team_id`
- `full_name`
- other roster metadata

Lazy-populated: a player row is inserted the first time someone follows them (or the first time they appear in a poll response). Names and team assignments are refreshed periodically from MLB roster endpoints for stored rows only.

If in-app player search or browse is added later, the table should be expanded to hold all ~1,500 active MLB players, refreshed daily. Without a search feature, storing only referenced players is sufficient.

**follows**
- `user_id` (FK)
- `player_id` (FK)
- `created_at`
- Primary key: `(user_id, player_id)`
- Index on `(player_id)` for fan-out lookup

Standard many-to-many join table. The per-player index is what makes fan-out queries cheap.

**notification_log**
- `event_id`
- `user_id`
- `player_id`
- `state` (at_bat, on_deck)
- `sent_at`
- `status`
- Unique constraint on `(event_id, user_id)` for idempotency

## Architecture

Four logical components. At MVP scale they can run on one host; each scales independently when needed.

### 1. Poller

Single instance, or leader-elected pair for HA. Polls MLB StatsAPI every 10s for active games. Only one process polls per game — duplicate polling wastes API quota and creates consistency problems. Use the diff endpoint to minimize payload.

### 2. Diff engine

Maintains last-known state per active game in Redis:

```
game:{game_id} → { at_bat_player_id, on_deck_player_id, updated_at }
```

On each poll, compares new game state against cached state. For each changed slot (`at_bat` or `on_deck`), emits one event to the queue. A single poll of one game can emit 0, 1, or 2 events.

All events share one schema; `state` is the discriminator:

```json
{
  "event_id": "01HXXXXXXXXXXXXXXXXXXXXXXX",
  "game_id": 746321,
  "player_id": 660271,
  "state": "at_bat",
  "occurred_at": "2026-04-22T19:32:14Z"
}
```

- `event_id`: ULID or monotonic ID, used for idempotency.
- `state`: enum — `at_bat` or `on_deck`.
- `player_id`: the player entering that state.
- `game_id`: included for debugging and deduplication.
- `occurred_at`: timestamp of the transition.

**Transition example.** Game 746321 cached state: `at_bat=A, on_deck=B`. Next poll returns `at_bat=B, on_deck=C`. Two events are emitted:

```json
{ "event_id": "...", "game_id": 746321, "player_id": B, "state": "at_bat",  "occurred_at": "..." }
{ "event_id": "...", "game_id": 746321, "player_id": C, "state": "on_deck", "occurred_at": "..." }
```

Player A's exit is not emitted — there is no "stopped batting" notification in scope. Cache is then updated to `at_bat=B, on_deck=C`.

If topic-based routing is used downstream, the topic name can be derived from `state` (e.g., one queue per state), but the payload schema remains uniform.

### 3. Fan-out workers

Stateless consumers. For each event, look up followers via the `follows` index and either enqueue per-user delivery jobs or publish to a topic (see below). Horizontally scalable.

### 4. Delivery workers

Call the push service. Handle retries, rate limits, and dead-lettering. Check the dedupe store (Redis TTL set or `notification_log` unique constraint) before sending; without this, a poller restart or queue redelivery will double-notify.

### Queue

Redis Streams or SQS between components. Kafka is unnecessary until well past MVP scale.

## Fan-out strategy

Do not send N individual API calls for N followers. Use topic-based push:

- **FCM** supports topics natively. When a user follows player 12345, subscribe their device token to topic `player_12345_atbat`. On transition, publish one message; FCM fans out to all subscribers. Peak API calls per 10s tick: ~30, not 300K.
- **APNs** has no native topic primitive. Either batch via HTTP/2 multiplexing with horizontal sharding, or route APNs through FCM (which proxies to APNs while preserving the topic model).
- **Discord** has per-webhook limits of 5 req/sec and a global bot limit of 50 req/sec. Adequate for MVP and personal use; not a production transport at scale. Ship it as the initial delivery adapter to unblock backend work, then replace with FCM.

The topic model moves fan-out cost to Google/Apple, which is what their infrastructure is built for.

## Idempotency

Every event has a monotonic `event_id`. Workers check `(event_id, user_id)` against a dedupe store before delivering. Use Redis with a short TTL (hours) for the hot path, backed by the `notification_log` unique constraint for durability.

## Stack

- **Language:** Python (FastAPI for the user-facing API)
- **Database:** Postgres
- **Cache / queue / dedupe:** Redis (Streams for queuing)
- **Push:** FCM primary, Discord adapter for MVP
- **Auth:** argon2id password hashing, standard session or JWT
- **Hosting:** Fly.io, Railway, Hetzner, or a single small EC2 instance until load requires more

Avoid Kubernetes until well past 10K active users.

## Operational notes

- Refresh stored `players` rows periodically from MLB roster endpoints to pick up team changes and name corrections.
- Per-tick work is a game-state diff against Redis, not a DB reload.
- Monitor MLB API error rates and latency; implement exponential backoff.
- Log every transition and delivery outcome for debugging.
- Rate-limit sign-ups and follow actions to prevent abuse.

## Scaling path

- **MVP (< 100 users):** Single host runs poller, workers, Postgres, Redis. Discord delivery.
- **Early production (< 10K users):** Separate Postgres and Redis onto managed services. Multiple worker processes. Add FCM.
- **Growth (< 100K users):** Dedicated poller host with HA pair. Multiple fan-out and delivery workers behind the queue.
- **Scale (~1M users):** Shard workers by player_id hash. Topic-based push does the heavy lifting; your infrastructure handles diff detection and event emission, which remains cheap.