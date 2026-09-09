"""Desktop channel delivery contract with real SQLite transactions."""

import pytest
from core.channels.protocol import InboundMsg
from core.db.engine import Base
from core.db.models import ChannelConnection, UserShadow
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db():
    from core.db.models.channel_relay import ChannelRelayDelivery, DesktopChannelBinding

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(UserShadow(user_id="owner", username="owner", extra_data={}))
        session.commit()
        yield session


def test_only_bound_device_can_claim_and_delivery_is_not_replayed(db):
    from core.services.channel_relay import ChannelRelayService

    svc = ChannelRelayService(db)
    grant = svc.register("owner", "device-a", "Laptop")
    binding = svc.bind("owner", grant["binding_id"])
    db.add(
        ChannelConnection(
            channel_id="bot",
            owner_user_id="owner",
            channel_type="lark",
            app_id="app",
            config={"desktop_execution": binding},
            enabled=True,
        )
    )
    db.commit()
    msg = InboundMsg(
        channel_id="bot",
        channel_type="lark",
        text="read local file",
        chat_type="p2p",
        external_conversation_id="peer",
        message_id="m1",
    )
    delivery = svc.enqueue(db.get(ChannelConnection, "bot"), msg)
    assert svc.claim("owner", "device-b", [grant["binding_id"]]) is None
    first = svc.claim("owner", "device-a", [grant["binding_id"]])
    assert first["delivery_id"] == delivery
    assert first["message"]["text"] == "read local file"
    assert svc.claim("owner", "device-a", [grant["binding_id"]]) is None
    assert svc.enqueue(db.get(ChannelConnection, "bot"), msg) == delivery
    assert svc.claim("owner", "device-a", [grant["binding_id"]]) is None


def test_binding_is_owner_scoped_and_single_use(db):
    from core.infra.exceptions import BadRequestError
    from core.services.channel_relay import ChannelRelayService

    svc = ChannelRelayService(db)
    grant = svc.register("owner", "d", "PC")
    with pytest.raises(BadRequestError):
        svc.bind("other", grant["binding_id"])
    svc.bind("owner", grant["binding_id"])
    with pytest.raises(BadRequestError):
        svc.bind("owner", grant["binding_id"])


def test_disabled_bot_cannot_continue_a_claimed_run(db):
    from core.infra.exceptions import AccessDeniedError
    from core.services.channel_relay import ChannelRelayService

    svc = ChannelRelayService(db)
    grant = svc.register("owner", "d", "PC")
    binding = svc.bind("owner", grant["binding_id"])
    conn = ChannelConnection(
        channel_id="bot",
        owner_user_id="owner",
        channel_type="lark",
        app_id="app",
        config={"desktop_execution": binding},
        enabled=True,
    )
    db.add(conn)
    db.commit()
    svc.enqueue(
        conn,
        InboundMsg(
            channel_id="bot",
            channel_type="lark",
            text="hello",
            chat_type="p2p",
            external_conversation_id="peer",
            message_id="one",
        ),
    )
    item = svc.claim("owner", "d", [grant["binding_id"]])
    conn.enabled = False
    db.commit()
    with pytest.raises(AccessDeniedError):
        svc.renew("owner", "d", item["delivery_id"], item["lease"])
