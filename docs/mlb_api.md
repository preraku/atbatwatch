# MLB Stats API Reference

Unofficial API — no SLA. Cache aggressively and back off on errors.

## Endpoints

**Today's schedule**
```
GET https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=MM/DD/YYYY&hydrate=team,linescore
```

**Live game feed (full)**
```
GET https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live
```

**Live game feed (diff patch)**
```
GET https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live/diffPatch?startTimecode=YYYYMMDD_HHMMSS
```
Returns only changed fields since `startTimecode`. The poller uses this to minimize payload; falls back to full feed when offense state changes.

**Player search**
```
GET https://statsapi.mlb.com/api/v1/people/search?names={name}
```
Response: `people[]` with `id`, `fullName`, `active`. Filter to `active=true` and exclude minor-leaguers.

## Offense state

Inside the live feed response at `liveData.linescore.offense`:

```json
{
  "batter":  { "id": 660271, "fullName": "Shohei Ohtani", "link": "..." },
  "onDeck":  { "id": 592450, "fullName": "Mookie Betts",  "link": "..." },
  "inHole":  { "id": 235,    "fullName": "...",            "link": "..." }
}
```

Keys may be absent if the slot is empty. The diff engine compares `batter.id` and `onDeck.id` against the Redis-cached previous state to detect transitions.

**Player disambiguation:** If a name search returns multiple active players, resolve with `atbatwatch lookup "Name"` (interactive picker) and pin the `player_id` in `config.toml`:

```toml
[[players]]
name = "Luis García Jr."
player_id = 671277   # disambiguates; 472610 is also active
```
