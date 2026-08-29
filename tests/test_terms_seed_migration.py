"""
alembic/versions/aa3c28032296_seed_baseline_terms.py 검증 (CLIAR-176).

이 프로젝트의 다른 테스트는 alembic 없이 Base.metadata.create_all로
스키마를 만들지만(마이그레이션 이력 자체는 검증 대상이 아니었으므로),
이번 seed data migration은 upgrade()/downgrade()의 실제 SQL 로직
(신규 seed / idempotent 재실행 / 기존 active row 보존·expire / downgrade의
member_agreement 참조 보호)이 핵심이므로, migration 모듈을 직접 import해
진짜 Alembic Operations 객체(SQLite in-memory 연결에 바인딩)로
upgrade()/downgrade()를 그대로 실행해 검증한다 — 로직을 테스트 코드에
다시 옮겨 적어 이중 관리하지 않는다.

migration 자체는 실제 대상 DB(PostgreSQL)를 기준으로 sa.text("now()")를
쓰므로(다른 migration 파일들과 동일한 관례), 이 SQLite 테스트 harness에서만
now() SQL 함수를 흉내내는 shim을 등록한다.

약관 원문 전체를 여기서 다시 비교하지 않는다(migration 파일과 이중 관리
방지). code/name/is_required/비어있지 않음/대표 문구 포함 여부만 검증한다.
"""

import datetime as dt
import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models.member_agreement import MemberAgreement, MemberAgreementAction
from app.models.terms import Terms
from app.models.user import MemberStatus, User

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "aa3c28032296_seed_baseline_terms.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "seed_baseline_terms_migration", _MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration_module()

LEGACY_EFFECTIVE_AT = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _register_now(dbapi_conn, _record):
        dbapi_conn.create_function(
            "now", 0, lambda: dt.datetime.now(dt.timezone.utc).isoformat()
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


@pytest.fixture()
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _run_upgrade(engine):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        MIGRATION.op = Operations(ctx)
        MIGRATION.upgrade()


def _run_downgrade(engine):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        MIGRATION.op = Operations(ctx)
        MIGRATION.downgrade()


def _active_rows(db_session):
    db_session.expire_all()
    return (
        db_session.execute(
            select(Terms).where(Terms.deleted_at.is_(None), Terms.expired_at.is_(None))
        )
        .scalars()
        .all()
    )


def _seed_legacy_terms_of_service(db_session, *, content="레거시 수동 입력 내용"):
    legacy = Terms(
        code="TERMS_OF_SERVICE",
        name="레거시 이용약관",
        content=content,
        is_required=True,
        effective_at=LEGACY_EFFECTIVE_AT,
    )
    db_session.add(legacy)
    db_session.commit()
    return legacy


def _seed_member(db_session, email="agreed@example.com", nickname="agreed-user"):
    member = User(
        member_id=uuid.uuid4(),
        email=email,
        nickname=nickname,
        status=MemberStatus.ACTIVE,
    )
    db_session.add(member)
    db_session.commit()
    return member


class TestUpgradeFromEmptyTable:
    """1. 빈 terms 상태 - 세 약관 seed 가능."""

    def test_seeds_exactly_the_three_expected_codes(self, engine, db_session):
        _run_upgrade(engine)

        codes = {row.code for row in _active_rows(db_session)}

        assert codes == {"TERMS_OF_SERVICE", "PRIVACY", "AI_ANALYSIS"}

    def test_is_required_matches_ticket_spec(self, engine, db_session):
        _run_upgrade(engine)

        by_code = {row.code: row.is_required for row in _active_rows(db_session)}

        assert by_code == {
            "TERMS_OF_SERVICE": True,
            "PRIVACY": True,
            "AI_ANALYSIS": False,
        }

    def test_content_is_real_text_not_a_placeholder(self, engine, db_session):
        _run_upgrade(engine)

        by_code = {row.code: row.content for row in _active_rows(db_session)}

        for code, content in by_code.items():
            assert len(content) > 200, f"{code} content looks like a placeholder"
        assert "Don't Paw-get Your Book" in by_code["TERMS_OF_SERVICE"]
        assert "제1조" in by_code["TERMS_OF_SERVICE"]
        assert "Don't Paw-get Your Book" in by_code["PRIVACY"]
        assert "Don't Paw-get Your Book" in by_code["AI_ANALYSIS"]


class TestUpgradeIdempotency:
    """2. 이미 동일한 active 약관이 존재 - 불필요한 중복 active row가 생기지 않음."""

    def test_rerunning_upgrade_does_not_create_duplicates(self, engine, db_session):
        _run_upgrade(engine)
        _run_upgrade(engine)
        _run_upgrade(engine)

        all_rows = db_session.execute(select(Terms)).scalars().all()
        active_rows = _active_rows(db_session)

        assert len(all_rows) == 3
        assert len(active_rows) == 3


class TestUpgradePreservesExistingDifferentActiveTerms:
    """
    3. 다른 내용의 active 약관이 존재 - 기존 row가 삭제/overwrite되지
    않음 / 기존 row expired 처리 / 새 row 생성 / active code는 하나만
    유지.
    """

    def test_legacy_row_is_kept_and_not_overwritten(self, engine, db_session):
        legacy = _seed_legacy_terms_of_service(db_session)
        legacy_id = legacy.id

        _run_upgrade(engine)

        db_session.expire_all()
        legacy_after = db_session.get(Terms, legacy_id)
        assert legacy_after is not None
        assert legacy_after.content == "레거시 수동 입력 내용"
        assert legacy_after.name == "레거시 이용약관"

    def test_legacy_row_is_expired_not_deleted(self, engine, db_session):
        legacy = _seed_legacy_terms_of_service(db_session)
        legacy_id = legacy.id

        _run_upgrade(engine)

        db_session.expire_all()
        legacy_after = db_session.get(Terms, legacy_id)
        assert legacy_after.deleted_at is None
        assert legacy_after.expired_at is not None

    def test_only_one_active_row_remains_for_the_code(self, engine, db_session):
        legacy = _seed_legacy_terms_of_service(db_session)
        legacy_id = legacy.id

        _run_upgrade(engine)

        active_tos = [
            row for row in _active_rows(db_session) if row.code == "TERMS_OF_SERVICE"
        ]
        assert len(active_tos) == 1
        assert active_tos[0].id != legacy_id
        assert "Don't Paw-get Your Book" in active_tos[0].content


class TestUpgradeSkipsFutureScheduledRow:
    """
    partial unique index(uk_terms_active_code)는 "expired_at IS NULL
    AND deleted_at IS NULL"인 행을 code당 1개만 허용하고, 여기에는
    effective_at 조건이 없다 — 즉 아직 시행되지 않은 미래 예약 행도
    그 slot을 차지할 수 있다. 이런 행을 baseline seed가 잘못
    expire/치환하면 안 된다(요청 사항: "미래 예약 약관을 임의로
    삭제/expire하지 말 것").
    """

    def test_future_row_is_left_untouched_and_no_baseline_is_inserted(
        self, engine, db_session
    ):
        future = Terms(
            code="TERMS_OF_SERVICE",
            name="차기 이용약관",
            content="아직 시행되지 않은 예정된 약관 내용",
            is_required=True,
            effective_at=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
        )
        db_session.add(future)
        db_session.commit()
        future_id = future.id

        _run_upgrade(engine)

        db_session.expire_all()
        future_after = db_session.get(Terms, future_id)
        assert future_after.expired_at is None
        assert future_after.deleted_at is None
        assert future_after.content == "아직 시행되지 않은 예정된 약관 내용"

        tos_rows = db_session.execute(
            select(Terms).where(Terms.code == "TERMS_OF_SERVICE")
        ).scalars().all()
        assert len(tos_rows) == 1, "baseline row must not be inserted for this code"

    def test_other_codes_still_seed_normally_when_one_code_has_a_future_row(
        self, engine, db_session
    ):
        future = Terms(
            code="TERMS_OF_SERVICE",
            name="차기 이용약관",
            content="아직 시행되지 않은 예정된 약관 내용",
            is_required=True,
            effective_at=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
        )
        db_session.add(future)
        db_session.commit()

        _run_upgrade(engine)

        db_session.expire_all()
        all_rows = db_session.execute(select(Terms)).scalars().all()
        by_code = {}
        for row in all_rows:
            by_code.setdefault(row.code, []).append(row)

        # TERMS_OF_SERVICE: 미래 예약 행 하나만 그대로 남아 있고
        # baseline은 추가되지 않았다.
        assert len(by_code["TERMS_OF_SERVICE"]) == 1
        assert by_code["TERMS_OF_SERVICE"][0].expired_at is None

        # 나머지 두 code는 baseline이 정상적으로 seed되었다.
        assert len(by_code["PRIVACY"]) == 1
        assert by_code["PRIVACY"][0].expired_at is None
        assert len(by_code["AI_ANALYSIS"]) == 1
        assert by_code["AI_ANALYSIS"][0].expired_at is None

    def test_row_effective_exactly_at_baseline_timestamp_is_also_skipped(
        self, engine, db_session
    ):
        """effective_at이 real now보다는 작거나 같아도(오늘이므로 D의
        첫 조건만으로는 안 걸림) _EFFECTIVE_AT과 같으면 expired_at을
        _EFFECTIVE_AT으로 설정할 때 ck_terms_effective_period(expired_at
        > effective_at)를 위반하게 되므로, 이 경우도 D로 처리되어
        건드리지 않아야 한다."""
        edge = Terms(
            code="PRIVACY",
            name="경계값 약관",
            content="경계값 테스트용 내용",
            is_required=True,
            effective_at=MIGRATION._EFFECTIVE_AT,
        )
        db_session.add(edge)
        db_session.commit()
        edge_id = edge.id

        _run_upgrade(engine)

        db_session.expire_all()
        edge_after = db_session.get(Terms, edge_id)
        assert edge_after.expired_at is None
        assert edge_after.content == "경계값 테스트용 내용"

        privacy_rows = db_session.execute(
            select(Terms).where(Terms.code == "PRIVACY")
        ).scalars().all()
        assert len(privacy_rows) == 1


class TestUpgradePreservesMemberAgreementHistory:
    """4. member_agreement - 기존 terms_id 관계를 변경하지 않음."""

    def test_existing_agreement_terms_id_is_untouched(self, engine, db_session):
        legacy = _seed_legacy_terms_of_service(db_session)
        member = _seed_member(db_session)
        agreement = MemberAgreement(
            member_id=member.member_id,
            terms_id=legacy.id,
            action=MemberAgreementAction.AGREE,
        )
        db_session.add(agreement)
        db_session.commit()
        agreement_id = agreement.id
        legacy_id = legacy.id

        _run_upgrade(engine)

        db_session.expire_all()
        agreement_after = db_session.get(MemberAgreement, agreement_id)
        assert agreement_after.terms_id == legacy_id


class TestDowngrade:
    def test_downgrade_removes_seeded_rows_and_restores_legacy_row(
        self, engine, db_session
    ):
        legacy = _seed_legacy_terms_of_service(db_session)
        legacy_id = legacy.id

        _run_upgrade(engine)
        _run_downgrade(engine)

        db_session.expire_all()
        remaining_codes = {row.code for row in db_session.execute(select(Terms)).scalars()}
        assert remaining_codes == {"TERMS_OF_SERVICE"}

        legacy_after = db_session.get(Terms, legacy_id)
        assert legacy_after.expired_at is None
        assert legacy_after.deleted_at is None

    def test_downgrade_from_empty_baseline_removes_all_three(self, engine, db_session):
        _run_upgrade(engine)

        _run_downgrade(engine)

        db_session.expire_all()
        remaining = db_session.execute(select(Terms)).scalars().all()
        assert remaining == []

    def test_downgrade_raises_when_member_agreement_references_a_seeded_row(
        self, engine, db_session
    ):
        _run_upgrade(engine)
        tos = db_session.execute(
            select(Terms).where(Terms.code == "TERMS_OF_SERVICE", Terms.expired_at.is_(None))
        ).scalar_one()
        member = _seed_member(db_session)
        agreement = MemberAgreement(
            member_id=member.member_id,
            terms_id=tos.id,
            action=MemberAgreementAction.AGREE,
        )
        db_session.add(agreement)
        db_session.commit()

        with pytest.raises(RuntimeError):
            _run_downgrade(engine)

        db_session.expire_all()
        still_present = db_session.get(Terms, tos.id)
        assert still_present is not None


class TestApiReturnsSeededBaseline:
    """5. API - GET /api/v1/terms 200, 세 code 존재, is_required 정확,
    content가 placeholder가 아닌 실제 seed 내용임."""

    def test_get_terms_returns_200_with_all_three_codes_in_order(
        self, client, engine, db_session
    ):
        _run_upgrade(engine)

        response = client.get("/api/v1/terms")

        assert response.status_code == 200
        assert [row["code"] for row in response.json()] == [
            "TERMS_OF_SERVICE",
            "PRIVACY",
            "AI_ANALYSIS",
        ]

    def test_is_required_matches_baseline_spec(self, client, engine, db_session):
        _run_upgrade(engine)

        body = client.get("/api/v1/terms").json()
        by_code = {row["code"]: row["is_required"] for row in body}

        assert by_code == {
            "TERMS_OF_SERVICE": True,
            "PRIVACY": True,
            "AI_ANALYSIS": False,
        }

    def test_content_contains_expected_baseline_markers(self, client, engine, db_session):
        _run_upgrade(engine)

        body = client.get("/api/v1/terms").json()
        by_code = {row["code"]: row["content"] for row in body}

        assert "Don't Paw-get Your Book" in by_code["TERMS_OF_SERVICE"]
        assert "제1조" in by_code["TERMS_OF_SERVICE"]
        assert "Don't Paw-get Your Book" in by_code["PRIVACY"]
        assert "Don't Paw-get Your Book" in by_code["AI_ANALYSIS"]
        for content in by_code.values():
            assert content and len(content) > 200
