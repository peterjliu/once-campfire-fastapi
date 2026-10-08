"""Action Cable protocol: authentication, signed streams, presence and live delivery."""

import json
import re

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from campfire import rails
from campfire.asgi import app
from tests.conftest import World

type Frame = dict[str, object]

PROTOCOL = ["actioncable-v1-json"]


def receive(socket: object) -> Frame:
    from starlette.testclient import WebSocketTestSession

    assert isinstance(socket, WebSocketTestSession)
    while True:
        frame = json.loads(socket.receive_text())
        assert isinstance(frame, dict)
        if frame.get("type") != "ping":
            return frame


def identifier(channel: str, **params: object) -> str:
    return json.dumps({"channel": channel, **params})


def room_stream(w: World) -> str:
    page = w.client.get(f"/rooms/{w.room.id}").text
    match = re.search(
        r'<turbo-cable-stream-source channel="RoomMessagesChannel" signed-stream-name="([^"]+)"',
        page,
    )
    assert match
    return match[1]


def test_room_subscription_receives_live_message_and_presence(w: World) -> None:
    stream = room_stream(w)
    room_id = identifier("RoomMessagesChannel", signed_stream_name=stream)
    presence = identifier("PresenceChannel", room_id=w.room.id)
    with w.client.websocket_connect("/cable", subprotocols=PROTOCOL) as socket:
        assert receive(socket) == {"type": "welcome"}
        socket.send_text(json.dumps({"command": "subscribe", "identifier": room_id}))
        assert receive(socket) == {"type": "confirm_subscription", "identifier": room_id}
        socket.send_text(json.dumps({"command": "subscribe", "identifier": presence}))
        assert receive(socket) == {"type": "confirm_subscription", "identifier": presence}
        assert w.scalar(
            "SELECT connections FROM memberships WHERE room_id=? AND user_id=?",
            w.room.id,
            w.admin.id,
        ) == 1
        response = w.post(f"/rooms/{w.room.id}/messages", {"message[body]": "live over cable"})
        assert response.status_code == 200
        frame = receive(socket)
        assert frame["identifier"] == room_id
        assert frame["message"] == response.text
    assert w.scalar(
        "SELECT connections FROM memberships WHERE room_id=? AND user_id=?", w.room.id, w.admin.id
    ) == 0


def test_forged_and_foreign_streams_are_rejected(w: World) -> None:
    forged = identifier("RoomMessagesChannel", signed_stream_name=room_stream(w)[:-1] + "0")
    private = identifier(
        "RoomMessagesChannel",
        signed_stream_name=rails.sign_stream(
            rails.stream_name("Rooms::Closed", w.private.id)
        ),
    )
    foreign_rooms = identifier(
        "Turbo::StreamsChannel",
        signed_stream_name=rails.sign_stream(rails.user_rooms_stream(w.member.id)),
    )
    own_rooms = identifier(
        "Turbo::StreamsChannel",
        signed_stream_name=rails.sign_stream(rails.user_rooms_stream(w.admin.id)),
    )
    w.foreign_write(
        "DELETE FROM memberships WHERE room_id=? AND user_id=?", w.private.id, w.admin.id
    )
    with w.client.websocket_connect("/cable", subprotocols=PROTOCOL) as socket:
        assert receive(socket) == {"type": "welcome"}
        for rejected in (forged, private, foreign_rooms):
            socket.send_text(json.dumps({"command": "subscribe", "identifier": rejected}))
            assert receive(socket) == {"type": "reject_subscription", "identifier": rejected}
        socket.send_text(json.dumps({"command": "subscribe", "identifier": own_rooms}))
        assert receive(socket) == {"type": "confirm_subscription", "identifier": own_rooms}


def test_signed_out_socket_is_refused(w: World) -> None:
    with TestClient(app) as anonymous, pytest.raises(WebSocketDisconnect):
        with anonymous.websocket_connect("/cable", subprotocols=PROTOCOL) as socket:
            socket.receive_text()


def test_cross_origin_socket_is_refused(w: World) -> None:
    with pytest.raises(WebSocketDisconnect):
        with w.client.websocket_connect(
            "/cable", subprotocols=PROTOCOL, headers={"origin": "http://attacker.test"}
        ) as socket:
            socket.receive_text()


def test_typing_notifications_reach_room_subscribers(w: World) -> None:
    typing = identifier("TypingNotificationsChannel", room_id=w.room.id)
    with w.client.websocket_connect("/cable", subprotocols=PROTOCOL) as socket:
        assert receive(socket) == {"type": "welcome"}
        socket.send_text(json.dumps({"command": "subscribe", "identifier": typing}))
        assert receive(socket) == {"type": "confirm_subscription", "identifier": typing}
        socket.send_text(
            json.dumps(
                {"command": "message", "identifier": typing, "data": json.dumps({"action": "start"})}
            )
        )
        assert receive(socket) == {
            "identifier": typing,
            "message": {"action": "start", "user": {"id": w.admin.id, "name": "Admin"}},
        }
