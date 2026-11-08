"""Rep Racer 1v1 rooms: the state machine, with a fake clock (no sleeping, no sockets, no database).

Pose data is SIMULATED by app.simulation; these tests prove the room rules (countdown, scoring, winner, duplicate
protection, disconnects), not that a real person's camera is counted accurately.
"""

from __future__ import annotations

import pytest

from app.schemas.games import RoomPose
from app.services import game_rooms
from app.services.exercises import resolve
from app.services.game_rooms import COUNTDOWN, FINISHED, LOBBY, RACING, GameRooms, Room, RoomError, normalize_code
from app.simulation import FRAME_MS, pushup_messages, squat_messages

SQUAT = resolve("squat")


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def pose(m: dict) -> RoomPose:
    return RoomPose.model_validate({k: v for k, v in m.items() if k != "session_id"})


def new_room(target: int = 3, limit: int = 60, exercise=SQUAT) -> tuple[Room, Clock]:
    clock = Clock()
    room = Room("ABC234", "alice", exercise, target, limit, now=clock)
    room.join("alice", "Alice")
    room.connect("alice", object())
    return room, clock


def seat_bob(room: Room) -> None:
    room.join("bob", "Bob")
    room.connect("bob", object())


def start_race(room: Room, clock: Clock) -> None:
    room.set_ready("alice", True)
    room.set_ready("bob", True)
    assert room.status == COUNTDOWN
    clock.t += game_rooms.COUNTDOWN_S
    assert room.tick() is True
    assert room.status == RACING


def play(room: Room, clock: Clock, user: str, messages) -> None:
    """Send frames at real-time pace: the fake clock moves 33 ms per frame."""
    for m in messages:
        clock.t += FRAME_MS / 1000
        room.on_pose(user, pose(m))


def events(room: Room, kind: str) -> list[dict]:
    return [e for _, e in room.outbox if e["type"] == kind]


# ------------------------------------------------------------------ joining


def test_room_codes_are_short_unique_and_easy_to_read():
    rooms = GameRooms()
    codes = {rooms.create(f"user-{i}", f"P{i}", SQUAT, 10, 60).code for i in range(40)}
    assert len(codes) == 40
    assert all(len(c) == game_rooms.CODE_LENGTH and set(c) <= set(game_rooms.CODE_ALPHABET) for c in codes)
    assert not any(ch in "01OI" for c in codes for ch in c)


def test_a_second_player_joins_and_a_third_is_refused():
    rooms = GameRooms()
    room = rooms.create("alice", "Alice", SQUAT, 10, 60)
    assert [p.name for p in room.players.values()] == ["Alice"] and room.host_id == "alice"
    rooms.join(room.code.lower(), "bob", "Bob")  # codes are case-insensitive
    assert len(room.players) == 2
    with pytest.raises(RoomError) as full:
        rooms.join(room.code, "carol", "Carol")
    assert full.value.status == 409 and full.value.code == "room_full"
    assert rooms.join(room.code, "bob", "Bob") is room  # joining again (a page reload) is fine


def test_unknown_and_malformed_codes_are_not_found():
    rooms = GameRooms()
    for bad in ("ZZZZZZ", "", "no way!", "AB"):
        with pytest.raises(RoomError) as e:
            rooms.join(bad, "bob", "Bob")
        assert e.value.status == 404
    with pytest.raises(RoomError):
        normalize_code("bad code")


def test_nobody_can_join_a_race_that_already_started():
    room, clock = new_room()
    seat_bob(room)
    start_race(room, clock)
    with pytest.raises(RoomError) as e:
        room.join("carol", "Carol")
    assert e.value.code == "race_in_progress"


def test_you_cannot_open_a_second_race_while_racing_but_a_lobby_is_simply_left():
    rooms = GameRooms()
    first = rooms.create("alice", "Alice", SQUAT, 10, 60)
    second = rooms.create("alice", "Alice", SQUAT, 10, 60)  # leaves the empty lobby: it is closed
    assert first.closed and first.code != second.code
    rooms.join(second.code, "bob", "Bob")
    second.connect("alice", object())
    second.connect("bob", object())
    second.set_ready("alice", True)
    second.set_ready("bob", True)
    with pytest.raises(RoomError) as e:
        rooms.create("alice", "Alice", SQUAT, 10, 60)
    assert e.value.code == "already_in_a_race"


# ------------------------------------------------------------------ ready and countdown


def test_the_countdown_needs_both_players_present_and_ready():
    room, clock = new_room()
    room.set_ready("alice", True)
    assert room.status == LOBBY  # alone
    seat_bob(room)
    assert room.status == LOBBY  # bob has not pressed ready
    room.set_ready("bob", True)
    assert room.status == COUNTDOWN
    assert room.snapshot()["starts_in_ms"] == 3000


def test_a_disconnected_player_cannot_be_ready():
    room, _ = new_room()
    seat_bob(room)
    bob_socket = room.players["bob"].socket
    room.set_ready("bob", True)
    room.disconnect("bob", bob_socket)
    assert room.players["bob"].ready is False and room.players["bob"].connected is False
    room.set_ready("alice", True)
    assert room.status == LOBBY


def test_both_players_see_the_same_synchronised_countdown_then_the_race_starts():
    room, clock = new_room()
    seat_bob(room)
    room.set_ready("alice", True)
    room.set_ready("bob", True)
    room.drain()
    clock.t += 1.0
    assert room.tick() is False and room.snapshot()["starts_in_ms"] == 2000  # relative time, same for both
    clock.t += 1.9
    assert room.tick() is False and room.status == COUNTDOWN
    clock.t += 0.2
    assert room.tick() is True and room.status == RACING
    started = events(room, "race_started")
    assert len(started) == 1 and started[0]["time_limit_s"] == 60 and started[0]["target_reps"] == 3
    assert all(to is None for to, e in room.outbox if e["type"] in ("race_started", "room_state"))  # to everyone


def test_leaving_during_the_countdown_cancels_it():
    room, clock = new_room()
    seat_bob(room)
    room.set_ready("alice", True)
    room.set_ready("bob", True)
    room.disconnect("bob", room.players["bob"].socket)
    assert room.status == LOBBY and not room.players["alice"].ready
    clock.t += 10
    assert room.tick() is False and room.status == LOBBY


def test_ready_after_the_countdown_began_changes_nothing():
    room, clock = new_room()
    seat_bob(room)
    start_race(room, clock)
    room.set_ready("bob", False)
    assert room.status == RACING


# ------------------------------------------------------------------ racing and scoring


def test_reps_are_counted_by_the_server_and_both_scores_stay_in_sync():
    room, clock = new_room(target=5)
    seat_bob(room)
    start_race(room, clock)
    room.drain()
    play(room, clock, "alice", squat_messages("x", reps=2))
    counted = events(room, "rep_counted")
    assert [(e["user_id"], e["reps"]) for e in counted] == [("alice", 1), ("alice", 2)]
    snap = room.snapshot()
    assert {p["user_id"]: p["reps"] for p in snap["players"]} == {"alice": 2, "bob": 0}
    assert room.players["alice"].rep_ms == sorted(room.players["alice"].rep_ms) and len(room.players["alice"].rep_ms) == 2


def test_first_to_the_target_wins_immediately_and_the_race_ends():
    room, clock = new_room(target=3)
    seat_bob(room)
    start_race(room, clock)
    play(room, clock, "bob", squat_messages("x", reps=1))
    play(room, clock, "alice", squat_messages("x", reps=5))
    assert room.status == FINISHED and room.winner_id == "alice" and room.end_reason == "target_reached"
    assert room.players["alice"].reps == 3  # the race stopped at the target: later frames are ignored
    by_user = {r["user_id"]: r for r in room.results}
    assert by_user["alice"]["result"] == "win" and by_user["alice"]["placement"] == 1
    assert by_user["bob"]["result"] == "loss" and by_user["bob"]["placement"] == 2 and by_user["bob"]["reps"] == 1
    assert room.persist_pending is True
    done = events(room, "race_finished")
    assert len(done) == 1 and done[0]["winner_id"] == "alice"


def test_time_up_gives_the_win_to_the_player_with_more_reps():
    room, clock = new_room(target=10, limit=30)
    seat_bob(room)
    start_race(room, clock)
    play(room, clock, "alice", squat_messages("x", reps=1))
    play(room, clock, "bob", squat_messages("x", reps=2))
    clock.t += 31
    assert room.tick() is True
    assert room.status == FINISHED and room.end_reason == "time_up" and room.winner_id == "bob"


def test_time_up_with_equal_reps_goes_to_whoever_got_there_first():
    room, clock = new_room(target=10, limit=60)
    seat_bob(room)
    start_race(room, clock)
    play(room, clock, "alice", squat_messages("x", reps=1))
    play(room, clock, "bob", squat_messages("x", reps=1))
    clock.t += 61
    room.tick()
    assert room.winner_id == "alice"  # her rep was counted earlier in the race


def test_time_up_with_nothing_counted_is_a_draw():
    room, clock = new_room(target=10, limit=10)
    seat_bob(room)
    start_race(room, clock)
    clock.t += 11
    room.tick()
    assert room.status == FINISHED and room.winner_id is None
    assert {r["result"] for r in room.results} == {"draw"}


def test_the_race_has_a_server_side_timer_even_if_nobody_sends_anything():
    room, clock = new_room(limit=20)
    seat_bob(room)
    start_race(room, clock)
    clock.t += 19
    assert room.tick() is False and room.snapshot()["remaining_ms"] == 1000
    clock.t += 1.5
    assert room.tick() is True and room.status == FINISHED


def test_a_replayed_frame_or_message_cannot_add_a_rep():
    room, clock = new_room(target=10)
    seat_bob(room)
    start_race(room, clock)
    frames = list(squat_messages("x", reps=1))
    play(room, clock, "alice", frames)
    assert room.players["alice"].reps == 1
    for _ in range(3):  # the same messages again: same sequence numbers and timestamps
        play(room, clock, "alice", frames)
    assert room.players["alice"].reps == 1


def test_frames_before_the_start_and_after_the_finish_never_count():
    room, clock = new_room(target=1)
    seat_bob(room)
    play(room, clock, "alice", squat_messages("x", reps=3))  # lobby
    room.set_ready("alice", True)
    room.set_ready("bob", True)
    play(room, clock, "alice", squat_messages("x", reps=3))  # countdown
    assert room.players["alice"].reps == 0
    clock.t += 3
    room.tick()
    play(room, clock, "alice", squat_messages("x", reps=3, start_sequence=1000, start_ts=1_794_100_000_000))
    assert room.status == FINISHED and room.players["alice"].reps == 1
    play(room, clock, "bob", squat_messages("x", reps=3, start_sequence=1000, start_ts=1_794_100_000_000))
    assert room.players["bob"].reps == 0  # the race is over


def test_a_stranger_cannot_send_frames_into_a_race():
    room, clock = new_room(target=3)
    seat_bob(room)
    start_race(room, clock)
    play(room, clock, "mallory", squat_messages("x", reps=3))
    assert room.status == RACING and sum(p.reps for p in room.players.values()) == 0


def test_the_wrong_movement_does_not_move_a_runner():
    room, clock = new_room(target=3)  # a squat race
    seat_bob(room)
    start_race(room, clock)
    play(room, clock, "alice", pushup_messages("x", reps=4))  # elbow angles in a knee race
    assert room.players["alice"].reps == 0


def test_a_pushup_race_counts_elbow_reps():
    room, clock = new_room(target=2, exercise=resolve("push-up"))
    seat_bob(room)
    start_race(room, clock)
    play(room, clock, "alice", pushup_messages("x", reps=2))
    assert room.winner_id == "alice" and room.exercise.slug == "push-up"


def test_each_player_is_coached_privately():
    room, clock = new_room(target=10)
    seat_bob(room)
    start_race(room, clock)
    room.drain()
    play(room, clock, "alice", squat_messages("x", reps=5, depths=[85, 85, 85, 99, 99]))
    cues = events(room, "coaching_feedback")
    assert cues and all(to == "alice" for to, e in room.outbox if e["type"] == "coaching_feedback")


# ------------------------------------------------------------------ disconnects and reconnects


def test_a_dropped_player_who_returns_in_time_keeps_their_progress():
    room, clock = new_room(target=5)
    seat_bob(room)
    start_race(room, clock)
    play(room, clock, "bob", squat_messages("x", reps=2))
    old = room.players["bob"].socket
    room.disconnect("bob", old)
    clock.t += game_rooms.RECONNECT_GRACE_S - 1
    assert room.tick() is False and room.status == RACING
    previous = room.connect("bob", object())  # a new tab / reconnect
    assert previous is None and room.players["bob"].connected and room.players["bob"].reps == 2
    play(room, clock, "bob", squat_messages("x", reps=1, start_sequence=9000, start_ts=1_796_000_000_000))
    assert room.players["bob"].reps == 3 and room.status == RACING  # and they can carry on counting


def test_a_late_disconnect_from_a_replaced_socket_does_not_kick_the_new_connection():
    room, _ = new_room()
    seat_bob(room)
    old = room.players["bob"].socket
    new = object()
    assert room.connect("bob", new) is old  # the caller closes the old one
    room.disconnect("bob", old)  # the old socket's cleanup arrives late
    assert room.players["bob"].connected and room.players["bob"].socket is new


def test_a_player_gone_longer_than_the_grace_period_forfeits():
    room, clock = new_room(target=5)
    seat_bob(room)
    start_race(room, clock)
    room.disconnect("bob", room.players["bob"].socket)
    clock.t += game_rooms.RECONNECT_GRACE_S + 1
    assert room.tick() is True
    assert room.status == FINISHED and room.end_reason == "forfeit" and room.winner_id == "alice"
    assert {r["user_id"]: r["result"] for r in room.results} == {"alice": "win", "bob": "forfeit"}


def test_leaving_mid_race_is_a_forfeit():
    room, clock = new_room(target=5)
    seat_bob(room)
    start_race(room, clock)
    room.leave("alice")
    assert room.status == FINISHED and room.winner_id == "bob" and room.end_reason == "forfeit"


def test_leaving_the_lobby_passes_hosting_on_and_the_last_one_out_closes_the_room():
    room, _ = new_room()
    seat_bob(room)
    room.leave("alice")
    assert room.host_id == "bob" and list(room.players) == ["bob"] and not room.closed
    room.leave("bob")
    assert room.closed


def test_a_reconnect_for_someone_not_in_the_room_is_refused():
    room, _ = new_room()
    with pytest.raises(RoomError) as e:
        room.connect("mallory", object())
    assert e.value.status == 404


# ------------------------------------------------------------------ rematch


def test_rematch_needs_both_players_and_starts_a_clean_race():
    room, clock = new_room(target=2)
    seat_bob(room)
    start_race(room, clock)
    play(room, clock, "alice", squat_messages("x", reps=2))
    assert room.status == FINISHED and room.race_id == 0
    room.request_rematch("alice")
    assert room.status == FINISHED  # waiting for bob
    room.request_rematch("bob")
    assert room.status == LOBBY and room.race_id == 1
    assert room.winner_id is None and room.results == []
    assert all(p.reps == 0 and not p.ready and not p.rematch for p in room.players.values())
    start_race(room, clock)  # and the next race works
    play(room, clock, "bob", squat_messages("x", reps=2, start_sequence=5000, start_ts=1_795_000_000_000))
    assert room.winner_id == "bob"


def test_rematch_is_ignored_before_the_race_is_over():
    room, clock = new_room()
    seat_bob(room)
    start_race(room, clock)
    room.request_rematch("alice")
    assert room.status == RACING and not room.players["alice"].rematch


# ------------------------------------------------------------------ housekeeping and the saved payload


def test_idle_and_closed_rooms_are_swept():
    clock = Clock()
    rooms = GameRooms(now=clock)
    idle = rooms.create("alice", "Alice", SQUAT, 10, 60)
    clock.t += game_rooms.IDLE_TTL_S + 1
    fresh = rooms.create("bob", "Bob", SQUAT, 10, 60)
    assert rooms.get(idle.code) is None and rooms.get(fresh.code) is fresh  # creating a room sweeps stale ones
    fresh.leave("bob")
    assert fresh.closed and rooms.sweep() == 1 and rooms.get(fresh.code) is None


def test_the_finished_payload_has_what_the_database_needs():
    room, clock = new_room(target=2)
    seat_bob(room)
    start_race(room, clock)
    play(room, clock, "alice", squat_messages("x", reps=2))
    payload = room.finished_payload()
    assert payload["winner_id"] == "alice" and payload["end_reason"] == "target_reached" and payload["target_reps"] == 2
    alice = next(p for p in payload["players"] if p["user_id"] == "alice")
    assert alice["reps"] == 2 and len(alice["rep_ms"]) == 2 and len(alice["rep_metrics"]) == 2
    assert all("ended_at" in m for m in alice["rep_metrics"])
    assert payload["duration_ms"] >= alice["rep_ms"][-1] - 1
