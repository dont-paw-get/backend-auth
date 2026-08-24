"""restructure member table for member_id uuid and status enum

Revision ID: 58da6f61f1f6
Revises: aa8dcb64d638
Create Date: 2026-08-24 15:19:50.113852

CLIAR-87: member 테이블을 확정된 최종 구조로 변경한다.

변경 내용:
- 기존 PK인 user_id(VARCHAR)를 member_id(UUID, NOT NULL UNIQUE)로
  전환하고, 새로운 내부 PK로 id(BIGINT IDENTITY)를 추가한다.
  기존 user_id 값은 Cognito sub 문자열이므로, 임의의 새 UUID를
  생성해서 대체하지 않고 CAST(user_id AS uuid)로 "그 값 자체를"
  UUID로 변환한다. Cognito sub와의 연결을 유지하기 위함이다.
  기존 user_id 값 중 UUID로 변환할 수 없는 값이 하나라도 있으면
  이 migration은 조용히 넘어가지 않고 명확하게 실패한다
  (PostgreSQL의 CAST 실패는 자동으로 예외를 발생시킨다).
- status를 String("PENDING" 기본값)에서 member_status ENUM
  ('ACTIVE', 'WITHDRAWN')으로 전환한다. 기존 PENDING 값은 ACTIVE로
  변환한다(최종 스키마에 PENDING이 존재하지 않기 때문).
- nickname의 UNIQUE 제약을 제거한다(닉네임 중복 허용).
- profile_image_url을 VARCHAR에서 TEXT로 변경한다.
- 다음 컬럼을 제거한다: representative_librarian_id, agree_terms,
  agree_privacy, agreed_at, agree_ai_analysis,
  ai_analysis_consent_updated_at.
  이 컬럼들이 제거되면 기존에 저장된 약관 동의 이력(TRUE/FALSE,
  동의 시각)은 삭제된다. 실제 약관 문구(terms.content)가 아직
  확정되지 않아 이번 migration에서는 이 데이터를 terms/
  member_agreement로 자동 이관하지 않는다(요구사항에 따름).
  운영 데이터에 이 컬럼들의 값이 존재한다면, 이 migration을
  실행하기 전에 별도로 백업/이관 여부를 반드시 확인해야 한다.

주의: downgrade()는 컬럼을 되살리지만, upgrade()에서 삭제된 값
(agree_terms 등의 실제 데이터)은 복구되지 않는다. status도 ACTIVE ->
PENDING으로 되돌리지 않고 그대로 ACTIVE로 남긴다(원래 어떤 행이
PENDING이었는지 downgrade 시점에는 알 수 없기 때문).

id(BIGINT IDENTITY) sequence 동기화:
새 PK id를 ROW_NUMBER()로 수동 채운 뒤, identity backing sequence를
setval()로 MAX(id)+1에 동기화한다. 이 단계가 없으면 sequence는 여전히
1을 가리키고 있어(수동 UPDATE는 sequence를 전진시키지 않음) migration
이후 첫 신규 회원 생성 시 PK 중복 오류가 발생한다. 기존 row가 0개인
경우도 COALESCE(..., 0) + 1로 안전하게 1부터 시작한다. member_id
(Cognito sub) 값은 이 과정에서 전혀 건드리지 않는다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '58da6f61f1f6'
down_revision: Union[str, None] = 'aa8dcb64d638'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


member_status_enum = sa.Enum("ACTIVE", "WITHDRAWN", name="member_status")


def upgrade() -> None:
    bind = op.get_bind()

    # 1) member_status ENUM 생성 (member 테이블이 이 타입을 사용하기 전에 존재해야 함)
    member_status_enum.create(bind, checkfirst=False)

    # 2) member_id(UUID) 컬럼 추가. 기존 user_id 값을 그대로 UUID로 CAST한다.
    #    user_id 중 UUID로 변환 불가능한 값이 있으면 여기서 예외가 발생하며
    #    migration이 중단된다(조용히 새 UUID로 대체하지 않음).
    op.add_column("member", sa.Column("member_id", sa.Uuid(), nullable=True))
    op.execute("UPDATE member SET member_id = CAST(user_id AS uuid)")
    op.alter_column("member", "member_id", nullable=False)
    op.create_unique_constraint("uq_member_member_id", "member", ["member_id"])

    # 3) 새 내부 PK(id)를 추가하기 전에 기존 PK(user_id) 제약을 제거한다.
    #    PostgreSQL은 테이블 rename(aa8dcb64d638: users -> member) 시 제약조건
    #    이름을 함께 바꾸지 않으므로, 실제 제약조건 이름은 여전히 users_pkey다.
    op.drop_constraint("users_pkey", "member", type_="primary")
    op.add_column(
        "member",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=True),
    )
    op.execute(
        "UPDATE member SET id = sub.rn FROM "
        "(SELECT user_id, ROW_NUMBER() OVER (ORDER BY user_id) AS rn FROM member) AS sub "
        "WHERE member.user_id = sub.user_id"
    )
    op.alter_column("member", "id", nullable=False)
    op.create_primary_key("member_pkey", "member", ["id"])

    # id를 수동으로 UPDATE했으므로, identity backing sequence를 다음
    # INSERT가 MAX(id)+1을 받도록 동기화한다. PostgreSQL의 identity
    # sequence는 nextval() 호출로만 전진하며 UPDATE로는 갱신되지 않으므로,
    # 이 단계를 생략하면 이후 신규 회원 생성 시 PK 충돌(duplicate key)이
    # 발생할 수 있다. sequence 이름은 하드코딩하지 않고
    # pg_get_serial_sequence로 동적으로 조회한다. 기존 row가 0개인
    # 경우(MAX(id)가 NULL)에도 COALESCE로 안전하게 1부터 시작하도록
    # 처리한다.
    op.execute(
        "SELECT setval("
        "pg_get_serial_sequence('member', 'id'), "
        "COALESCE((SELECT MAX(id) FROM member), 0) + 1, "
        "false"
        ")"
    )

    # 4) user_id(VARCHAR) 컬럼은 더 이상 필요하지 않으므로 제거한다.
    #    (기존 unique/PK 제약은 위에서 이미 제거됨. email/nickname unique는 유지)
    op.drop_column("member", "user_id")

    # 5) nickname UNIQUE 제거
    op.drop_constraint("uq_users_nickname", "member", type_="unique")

    # 6) profile_image_url VARCHAR -> TEXT
    op.alter_column(
        "member",
        "profile_image_url",
        existing_type=sa.String(),
        type_=sa.Text(),
        existing_nullable=True,
    )

    # 7) status: String -> member_status ENUM, 기존 PENDING -> ACTIVE
    op.execute("UPDATE member SET status = 'ACTIVE' WHERE status = 'PENDING'")
    op.alter_column(
        "member",
        "status",
        existing_type=sa.String(),
        type_=member_status_enum,
        existing_nullable=False,
        postgresql_using="status::member_status",
    )
    op.alter_column("member", "status", server_default=None)

    # 8) 약관/대표사서 관련 컬럼 제거.
    #    주의: 이 컬럼들에 저장된 기존 데이터(동의 여부/시각)는 이
    #    migration으로 영구 삭제된다. terms/member_agreement로의
    #    자동 이관은 수행하지 않는다(요구사항 및 결과 보고 참고).
    op.drop_column("member", "representative_librarian_id")
    op.drop_column("member", "agree_terms")
    op.drop_column("member", "agree_privacy")
    op.drop_column("member", "agreed_at")
    op.drop_column("member", "agree_ai_analysis")
    op.drop_column("member", "ai_analysis_consent_updated_at")


def downgrade() -> None:
    bind = op.get_bind()

    # 컬럼 복원(단, 기존 데이터 값 자체는 복구되지 않는다).
    #
    # 원본 migration(53a0fb49dca7)에서 agree_terms/agree_privacy/agreed_at
    # 은 NOT NULL이었다. downgrade에서도 스키마 구조(nullable 여부)를
    # 가능한 한 원래와 일치시키기 위해 NOT NULL로 복원한다. 다만 과거에
    # 실제로 어떤 값이었는지는 이 시점에 알 수 없으므로(upgrade에서
    # 이미 컬럼 자체가 삭제됨), NOT NULL 제약을 만족시키기 위한 임시
    # backfill 값을 사용한다. 이 값은 스키마 구조 복원용일 뿐이며,
    # 과거 실제 약관 동의 이력을 복구하거나 재현하는 것이 아니다.
    op.add_column("member", sa.Column("ai_analysis_consent_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "member",
        sa.Column("agree_ai_analysis", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # agreed_at: 원본은 NOT NULL이었다. 실제 과거 동의 시각은 복구할 수
    # 없으므로, 이 downgrade가 실행되는 시점의 now()를 스키마 복원용
    # placeholder로 backfill한다(실제 동의 발생 시각이 아님).
    op.add_column("member", sa.Column("agreed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE member SET agreed_at = now() WHERE agreed_at IS NULL")
    op.alter_column("member", "agreed_at", nullable=False)

    # agree_privacy/agree_terms: 원본은 NOT NULL이었다. 과거 실제 동의
    # 여부는 복구할 수 없으므로, NOT NULL 제약을 만족시키기 위한 값으로
    # False를 backfill한다. 이 값은 "동의하지 않았다"는 사실을 의미하는
    # 것이 아니라 단순히 스키마 구조(NOT NULL) 복원을 위한 placeholder다.
    op.add_column("member", sa.Column("agree_privacy", sa.Boolean(), nullable=True))
    op.execute("UPDATE member SET agree_privacy = false WHERE agree_privacy IS NULL")
    op.alter_column("member", "agree_privacy", nullable=False)

    op.add_column("member", sa.Column("agree_terms", sa.Boolean(), nullable=True))
    op.execute("UPDATE member SET agree_terms = false WHERE agree_terms IS NULL")
    op.alter_column("member", "agree_terms", nullable=False)

    op.add_column("member", sa.Column("representative_librarian_id", sa.String(), nullable=True))

    # status: ENUM -> String (PENDING으로의 복원은 하지 않음, ACTIVE 값 그대로 문자열화)
    op.alter_column(
        "member",
        "status",
        existing_type=member_status_enum,
        type_=sa.String(),
        existing_nullable=False,
        postgresql_using="status::text",
    )
    op.alter_column("member", "status", server_default="PENDING")

    # profile_image_url TEXT -> VARCHAR
    op.alter_column(
        "member",
        "profile_image_url",
        existing_type=sa.Text(),
        type_=sa.String(),
        existing_nullable=True,
    )

    # nickname UNIQUE 복원
    op.create_unique_constraint("uq_users_nickname", "member", ["nickname"])

    # user_id(VARCHAR) 컬럼 복원, member_id를 문자열로 되돌려 채운다.
    op.add_column("member", sa.Column("user_id", sa.String(), nullable=True))
    op.execute("UPDATE member SET user_id = member_id::text")
    op.alter_column("member", "user_id", nullable=False)

    # PK를 user_id로 되돌린다.
    op.drop_constraint("member_pkey", "member", type_="primary")
    op.drop_column("member", "id")
    op.create_primary_key("member_pkey", "member", ["user_id"])

    # member_id 컬럼/제약 제거
    op.drop_constraint("uq_member_member_id", "member", type_="unique")
    op.drop_column("member", "member_id")

    member_status_enum.drop(bind, checkfirst=False)
