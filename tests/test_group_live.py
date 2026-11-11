import io
import json
from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest

from fortnite_chest_counter import discord_group as group
from fortnite_chest_counter import discord_scoreboard as board


WEBHOOK = group.Webhook("123456789012345678", "fake-webhook-token")
FINGERPRINT = "a" * 64
CHANNEL = SimpleNamespace(id=345678901234567890)


class Response(io.BytesIO):
    status = 200


def live_message(revision, total, message_id=100, player="Alex"):
    content = json.dumps([FINGERPRINT, player, revision, total, total, 1.0, 1794340000.0])
    return SimpleNamespace(id=message_id, channel=CHANNEL, webhook_id=WEBHOOK.id,
                           content=f"||FCCL1 {content}||")


def final_message(total, message_id=102):
    content = json.dumps(["b" * 32, "Alex", total, total, {}])
    embed = discord.Embed.from_dict({"footer": {
        "text": "FCC session v1 " + json.dumps([FINGERPRINT, 1.0])}})
    return SimpleNamespace(id=message_id, channel=CHANNEL, webhook_id=WEBHOOK.id,
                           content=f"||FCC1 {content}||", embeds=[embed])


def test_live_revisions_update_one_session_and_final_dominates(tmp_path):
    store = group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
    assert store.ingest(live_message(1, 2))
    store.advance_cursor(CHANNEL.id, 101)
    assert store.ingest(live_message(2, 5))
    assert not store.ingest(live_message(1, 2, message_id=100))
    assert store.cursor(CHANNEL.id) == 101
    session, = store.sessions()
    assert (session.total, session.certain, session.complete) == (5, 5, False)
    assert store.live_messages(CHANNEL.id) == (100,)
    assert store.ingest(final_message(6))
    assert not store.ingest(live_message(3, 200, message_id=103))
    session, = store.sessions()
    assert (session.total, session.complete) == (6, True)
    assert store.stats("Alex").matches == 1


def test_live_outbox_coalesces_and_patches_one_message(tmp_path, monkeypatch):
    store = group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response(b'{"id":"234567890123456789"}')

    monkeypatch.setattr(group, "build_opener", lambda *_: Opener())
    assert store.queue_live(FINGERPRINT, "Alex", 1, 2, 1.0)
    assert store.queue_live(FINGERPRINT, "Alex", 3, 4, 1.0)
    assert store.flush(WEBHOOK) == 1
    assert requests[-1].method == "POST"
    body = json.loads(requests[-1].data)
    assert body["username"] == "Chest Counter"
    assert body["avatar_url"] == group.webhook_identity()["avatar_url"]
    snapshot = group._parse_live(json.loads(requests[-1].data)["content"])
    assert snapshot[1].total == 4
    assert store.queue_live(FINGERPRINT, "Alex", 4, 6, 1.0)
    assert store.flush(WEBHOOK) == 1
    assert requests[-1].method == "PATCH"
    assert "username" not in json.loads(requests[-1].data)
    assert "avatar_url" not in json.loads(requests[-1].data)
    assert requests[-1].full_url.endswith("/messages/234567890123456789")
    assert not store.queue_live(FINGERPRINT, "Alex", 4, 6, 1.0)


def test_local_finish_retires_live_outbox(tmp_path):
    store = group.GroupStore(WEBHOOK.id, tmp_path / "group.db")
    store.queue_live(FINGERPRINT, "Alex", 1, 2, 1.0)
    match = SimpleNamespace(snapshot="finished", total=3, certain=2, chest_types={},
                            session_fingerprint=FINGERPRINT, boost=1.0)
    assert store.queue(match, "Alex")
    assert not store.queue_live(FINGERPRINT, "Alex", 3, 4, 1.0)
    session, = store.sessions()
    assert session.total == 3 and session.complete
    with store.connection() as db:
        assert db.execute("SELECT sent FROM live_outbox").fetchone() == (1,)


def test_guest_session_merge_is_validated_and_final_cannot_regress(tmp_path):
    store = group.GroupStore(WEBHOOK.id, tmp_path / "guest.db")
    current = group.SessionPlayer("Alex", 2, 3, 1.0, revision=2, updated_at=1794340000.0)
    session = group.TeamSession(FINGERPRINT, (current,))
    store.merge_sessions((session,))
    store.merge_sessions((replace(session, players=(replace(current, revision=1, total=2),)),))
    assert store.sessions() == (session,)
    finished = replace(session, players=(replace(current, total=5, finished=True),))
    store.merge_sessions((finished,))
    store.merge_sessions((replace(session, players=(replace(current, revision=500, total=100),)),))
    assert store.sessions() == (finished,)
    invalid = replace(session, fingerprint="unknown")
    with pytest.raises(ValueError):
        store.merge_sessions((replace(session, fingerprint="c" * 64), invalid))
    assert store.sessions() == (finished,)


def test_snapshot_attachment_round_trip_and_host_validation(monkeypatch):
    sessions = (group.TeamSession(FINGERPRINT, (
        group.SessionPlayer("Alex", 2, 4, 1.0, True),
        group.SessionPlayer("Sam", 3, 7, 2.0, False, 5, 1794340000.0),
    )),)
    raw = board._encode_sessions(sessions)
    assert board._decode_sessions(raw) == sessions
    message = {"attachments": [{"filename": "fcc-sessions.json", "size": len(raw),
                               "url": "https://cdn.discordapp.com/attachments/123/456/fcc-sessions.json?ex=1"}]}

    class Opener:
        def open(self, request, timeout):
            return Response(raw)

    monkeypatch.setattr(board, "build_opener", lambda *_: Opener())
    assert board._fetch_sessions(message) == sessions
    message["attachments"][0]["url"] = "https://example.com/attachments/123/456/fcc-sessions.json"
    monkeypatch.setattr(board, "build_opener", lambda *_: pytest.fail("Untrusted URL was requested"))
    with pytest.raises(ValueError):
        board._fetch_sessions(message)
    with pytest.raises(ValueError):
        board._decode_sessions(raw.replace(b'"total":4', b'"total":-4'))


def test_optional_sessions_fetch_preserves_existing_return_forms(monkeypatch):
    sessions = (group.TeamSession(FINGERPRINT, (group.SessionPlayer("Alex", 1, 2, 1.5),)),)
    message = {"webhook_id": WEBHOOK.id, "embeds": [board.make_embed(())]}
    monkeypatch.setattr(board, "_request", lambda *_: message)
    monkeypatch.setattr(board, "_fetch_sessions", lambda _: sessions)
    assert board.fetch(WEBHOOK, "234567890123456789") == ()
    assert board.fetch(WEBHOOK, "234567890123456789", with_team=True) == ((), None)
    assert board.fetch(WEBHOOK, "234567890123456789", with_sessions=True) == ((), None, None, sessions)


def test_live_session_chance_uses_each_players_boost():
    session = group.TeamSession(FINGERPRINT, (
        group.SessionPlayer("Alex", 1, 4, 1.0), group.SessionPlayer("Sam", 3, 7, 2.0)))
    probability = 1 - (1 - 1 / group.PIRATE_TOKEN_RATE) ** 4 * (1 - 2 / group.PIRATE_TOKEN_RATE) ** 7
    assert session.chance_percent == f"{probability:.6%}"
    assert session.total == 11 and session.certain == 4

def test_session_upload_uses_multipart_with_no_mentions(monkeypatch):
    sessions = (group.TeamSession(FINGERPRINT, (group.SessionPlayer("Alex", 2, 3, 1.0),)),)
    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            return Response(b'{"id":"234567890123456789","channel_id":"345678901234567890"}')

    monkeypatch.setattr(board, "build_opener", lambda *_: Opener())
    assert board.publish(WEBHOOK, "", (), sessions=sessions)[0] == "234567890123456789"
    request, = sent
    assert request.get_header("Content-type").startswith("multipart/form-data; boundary=fcc-")
    assert b'name="payload_json"' in request.data
    assert b'name="files[0]"; filename="fcc-sessions.json"' in request.data
    assert b'"allowed_mentions": {"parse": []}' in request.data
    assert b'"username": "Chest Counter"' in request.data
    assert group.webhook_identity()["avatar_url"].encode() in request.data
    assert board._encode_sessions(sessions) in request.data


def test_oversize_or_invalid_snapshot_is_rejected_before_upload(monkeypatch):
    session = group.TeamSession(FINGERPRINT, (group.SessionPlayer("Alex", 1, 2, 1.0),))
    monkeypatch.setattr(board, "_MAX_SESSION_BYTES", 20)
    with pytest.raises(ValueError, match="snapshot size"):
        board._encode_sessions((session,))
    with pytest.raises(ValueError):
        board._decode_sessions(b'{"format":true,"sessions":[]}')

def test_data_only_feed_message_hides_ranking_and_recovers_full_snapshot(monkeypatch):
    sessions = (group.TeamSession(FINGERPRINT, (group.SessionPlayer("Alex", 2, 3, 1.5, True),)),)
    ranking = (board.Row("Alex", 3, 1, 2, 1, {"rare": 2}),)
    team = group.TeamSummary(1, 1, 3, 30556, "0.003273%")
    settings = ("Duo", 1.5)
    response = {"id": "234567890123456789", "channel_id": "345678901234567890", "webhook_id": WEBHOOK.id}
    uploaded = []

    def request(_, suffix, method, payload=None, *, attachment=None):
        if payload:
            response.update(payload)
        if attachment is not None:
            uploaded.append(attachment)
        return response

    monkeypatch.setattr(board, "_request", request)
    monkeypatch.setattr(board, "_fetch_attachment", lambda _: uploaded[-1])
    board.publish(WEBHOOK, "", ranking, team, settings=settings, sessions=sessions, data_only=True)
    embed, = response["embeds"]
    assert embed["title"] == "Group sync data"
    assert embed["fields"] == []
    assert "Top 10" not in embed["description"] and "Alex" not in embed["description"]
    assert board.fetch(WEBHOOK, response["id"]) == ranking
    assert board.fetch(WEBHOOK, response["id"], with_team=True) == (ranking, team)
    assert board.fetch(WEBHOOK, response["id"], with_settings=True) == (ranking, team, settings)
    assert board.fetch(WEBHOOK, response["id"], with_sessions=True) == (ranking, team, settings, sessions)
    # The existing public message can be migrated to data-only without downloading its previous snapshot.
    board.publish(WEBHOOK, response["id"], ranking, team, settings=settings, sessions=sessions, data_only=True)


@pytest.mark.parametrize(("key", "value"), [
    ("ranking", [{"name": "Alex", "total": True, "matches": 1, "certain": 0, "estimated": 1, "chest_types": {}}]),
    ("ranking", [{"name": "Alex", "total": -1, "matches": 1, "certain": 0, "estimated": 1, "chest_types": {}}]),
    ("ranking", [{"name": "Alex", "total": 1, "matches": 1, "certain": 0, "estimated": 1, "chest_types": {"wrong": 1}}]),
    ("team", {"matches": 0, "players": 1, "chests": 9, "one_in": None, "chance_percent": "0.000000%"}),
    ("settings", ["Duo", True]),
])
def test_data_only_snapshot_rejects_invalid_board_state(key, value):
    document = json.loads(board._encode_data_snapshot((), None, None, ()))
    document[key] = value
    with pytest.raises(ValueError):
        board._decode_data_snapshot(json.dumps(document).encode())


def test_reset_leaderboard_has_no_ghost_team_summary_but_keeps_sessions(tmp_path):
    store = group.GroupStore(WEBHOOK.id, tmp_path / "reset.db")
    assert store.ingest(final_message(6))
    assert store.board()[1].matches == 1
    store.reset_players(["Alex"])
    ranking, team = store.board()
    assert ranking == [] and team == group.TeamSummary()
    session, = store.sessions()
    assert session.total == 6 and session.complete

def test_final_webhook_contribution_has_custom_sender_without_changing_destination(monkeypatch, tmp_path):
    monkeypatch.setattr(group.paths, "APP_DIR", tmp_path)
    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            return Response(b"{}")

    monkeypatch.setattr(group, "build_opener", lambda *_: Opener())
    group._publish(WEBHOOK, "b" * 32, "Alex", 3, 2, {}, FINGERPRINT, 1.0)
    request, = sent
    payload = json.loads(request.data)
    assert request.full_url == f"{WEBHOOK.url}?wait=true"
    assert payload["username"] == "Chest Counter"
    assert payload["avatar_url"].endswith("/main/docs/previews/logo.png")
    assert payload["allowed_mentions"] == {"parse": []}

def test_webhook_identity_uses_saved_name_and_avatar_and_allows_webhook_default(monkeypatch):
    from fortnite_chest_counter import settings
    options = {"webhook_name": "My Squad", "webhook_avatar_url": "https://example.org/squad.png"}
    monkeypatch.setattr(settings, "load_config", lambda _: ({"app": options}, "", []))
    assert group.webhook_identity() == {"username": "My Squad", "avatar_url": "https://example.org/squad.png"}
    options["webhook_avatar_url"] = ""
    assert group.webhook_identity() == {"username": "My Squad"}
    options["webhook_avatar_url"] = "file:///private/icon.png"
    assert group.webhook_identity() == {"username": "My Squad"}
    options["webhook_name"] = ""
    assert group.webhook_identity() == {"username": "Chest Counter"}
