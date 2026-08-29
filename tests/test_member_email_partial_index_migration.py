"""
alembic/versions/205eb1a0a7eb_*.py 검증 (CLIAR-177).

이 migration의 핵심 동작(uq_users_email DROP + partial unique index
CREATE)은 SQLite로 재현할 수 없다 — SQLite는 named UNIQUE 제약을
ALTER로 DROP하는 것을 지원하지 않는다(batch mode로만 가능하며, batch
mode는 "테이블을 통째로 재생성"하는 방식이라 실제 PostgreSQL
DDL과는 다른 경로다). 직접 확인:

    op.drop_constraint("uq_users_email", "member", type_="unique")
    -> NotImplementedError: No support for ALTER of constraints in
       SQLite dialect.

따라서 upgrade()는 이 테스트 스위트에서 검증하지 않는다 — DEV
PostgreSQL에 실제로 migration을 적용해 확인해야 한다(최종 보고 참고).

다만 downgrade()의 중복 이메일 감지 로직(순수 SELECT ... GROUP BY ...
HAVING COUNT(*) > 1 — 어떤 ALTER도 수행하기 전에 먼저 실행됨)은
SQLite에서도 그대로 재현 가능하므로, migration 파일을 다시 옮겨
적지 않고 실제 파일을 그대로 실행해 검증한다.
"""

import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.user import MemberStatus, User

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "205eb1a0a7eb_partial_unique_index_for_active_member_.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "member_email_partial_index_migration", _MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration_module()


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(engine):
    session = Session(bind=engine)
    yield session
    session.close()


def _run_downgrade(engine):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        MIGRATION.op = Operations(ctx)
        MIGRATION.downgrade()


def _create_member(db_session, *, email, status, deleted_at=None, nickname=None):
    member = User(
        member_id=uuid.uuid4(),
        email=email,
        nickname=nickname or f"nick-{uuid.uuid4().hex[:8]}",
        status=status,
        deleted_at=deleted_at,
    )
    db_session.add(member)
    db_session.commit()
    return member


class TestDowngradeDuplicateDetection:
    def test_raises_when_a_withdrawn_and_a_live_row_share_an_email(
        self, engine, db_session
    ):
        """가장 흔한 실제 시나리오: 탈퇴 완료 회원 이메일로 재가입이
        일어난 뒤 downgrade를 시도하는 경우."""
        _create_member(
            db_session,
            email="dup@example.com",
            status=MemberStatus.WITHDRAWN,
            deleted_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        _create_member(
            db_session, email="dup@example.com", status=MemberStatus.PENDING
        )

        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            _run_downgrade(engine)

    def test_raises_when_multiple_withdrawn_rows_share_an_email(
        self, engine, db_session
    ):
        for i in range(3):
            _create_member(
                db_session,
                email="dup@example.com",
                status=MemberStatus.WITHDRAWN,
                deleted_at=datetime.now(timezone.utc) - timedelta(days=i + 1),
            )

        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            _run_downgrade(engine)

    def test_error_message_reports_the_duplicate_count(self, engine, db_session):
        _create_member(
            db_session,
            email="a@example.com",
            status=MemberStatus.WITHDRAWN,
            deleted_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        _create_member(db_session, email="a@example.com", status=MemberStatus.ACTIVE)
        _create_member(
            db_session,
            email="b@example.com",
            status=MemberStatus.WITHDRAWN,
            deleted_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        _create_member(db_session, email="b@example.com", status=MemberStatus.PENDING)

        with pytest.raises(RuntimeError, match=r"2 email\(s\)"):
            _run_downgrade(engine)

    def test_does_not_delete_or_modify_any_row_before_raising(
        self, engine, db_session
    ):
        old = _create_member(
            db_session,
            email="dup@example.com",
            status=MemberStatus.WITHDRAWN,
            deleted_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        old_deleted_at = old.deleted_at
        new = _create_member(
            db_session, email="dup@example.com", status=MemberStatus.PENDING
        )

        with pytest.raises(RuntimeError):
            _run_downgrade(engine)

        db_session.expire_all()
        old_after = db_session.get(User, old.id)
        new_after = db_session.get(User, new.id)
        assert old_after is not None
        assert old_after.deleted_at == old_deleted_at
        assert old_after.email == "dup@example.com"
        assert new_after is not None
        assert new_after.email == "dup@example.com"
