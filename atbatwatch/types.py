from typing import NotRequired, TypedDict

# --- Shared ---


class _NamedTeam(TypedDict):
    id: int
    name: str


# --- /api/v1/schedule response ---


class _ScheduleTeamEntry(TypedDict):
    team: _NamedTeam


class _ScheduleTeams(TypedDict):
    home: _ScheduleTeamEntry
    away: _ScheduleTeamEntry


class _GameStatus(TypedDict):
    abstractGameState: str


class ScheduleGame(TypedDict):
    gamePk: int
    teams: _ScheduleTeams
    status: _GameStatus


class _DateEntry(TypedDict):
    games: list[ScheduleGame]


class ScheduleResponse(TypedDict):
    dates: list[_DateEntry]


# --- /api/v1.1/game/{gamePk}/feed/live response ---


class OffensePlayer(TypedDict, total=False):
    id: int
    fullName: str


class Offense(TypedDict, total=False):
    batter: OffensePlayer
    onDeck: OffensePlayer
    inHole: OffensePlayer


class _Linescore(TypedDict):
    offense: NotRequired[Offense]
    currentInning: NotRequired[int]
    isTopInning: NotRequired[bool]
    outs: NotRequired[int]


class _LiveData(TypedDict):
    linescore: _Linescore


class _LiveFeedTeam(TypedDict):
    id: int
    name: str


class _LiveFeedTeams(TypedDict):
    home: _LiveFeedTeam
    away: _LiveFeedTeam


class _LiveFeedStatus(TypedDict, total=False):
    abstractGameState: str
    detailedState: str
    codedGameState: str


class _GameData(TypedDict):
    teams: _LiveFeedTeams
    status: NotRequired[_LiveFeedStatus]


class _MetaData(TypedDict, total=False):
    timeStamp: str
    wait: int
    gameEvents: list[str]
    logicalEvents: list[str]


class LiveFeedResponse(TypedDict):
    gamePk: int
    metaData: _MetaData
    gameData: _GameData
    liveData: _LiveData


# --- /api/v1/people/search  (people[] elements) ---


class PersonSearchResult(TypedDict):
    id: int
    fullName: str
    active: NotRequired[bool]


# --- /api/v1/people/{player_id}  (people[0]) ---


class _PersonTeam(TypedDict, total=False):
    id: int
    name: str
    parentOrgId: int


class _PersonPosition(TypedDict, total=False):
    abbreviation: str


class PersonDetail(TypedDict):
    id: int
    fullName: str
    currentTeam: NotRequired[_PersonTeam]
    primaryPosition: NotRequired[_PersonPosition]
