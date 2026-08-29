"""seed baseline demo terms (TERMS_OF_SERVICE / PRIVACY / AI_ANALYSIS)

Revision ID: aa3c28032296
Revises: 9b41c7d2e5f3
Create Date: 2026-08-29 00:00:00.000000

CLIAR-176: 저장소에 실제 약관 원문이 없어 deployment마다 수동 DB 입력이
필요한 문제를 없애기 위해, 프로젝트/시연에서 실제로 사용할 수 있는
기본 약관 3종(TERMS_OF_SERVICE / PRIVACY / AI_ANALYSIS)을 data
migration으로 seed한다.

Project/demo baseline terms. Replace with reviewed production terms
before production launch. 법무 검토를 받은 최종 상용 약관이 아니다.

버전 이력 보존 정책 (app/models/terms.py 및 6acf46a83978 migration의
설계를 그대로 따름 — content를 직접 UPDATE로 덮어쓰지 않는다):

partial unique index(uk_terms_active_code)는 "expired_at IS NULL AND
deleted_at IS NULL"인 행을 code당 최대 1개만 허용한다 — effective_at은
이 index의 조건에 없다. 반면 application이 실제로 사용하는 "현재
적용 중"의 정의(app/repositories/terms_repository.py의
get_current_by_code/list_current)는 여기에 "effective_at <= now"가
추가된다. 즉 index가 허용하는 "이 code의 유일한 slot"에는 이미 시행된
행뿐 아니라 아직 시행 전인 미래 예약 행도 들어갈 수 있다 — 이 둘을
구분하지 않으면 미래 예약 행을 잘못 건드리게 되므로, 이 migration은
code별로 그 slot의 행을 조회한 뒤 effective_at으로 한 번 더 분기한다.

  A. slot에 행이 없음
     -> 이번 baseline 약관을 새 row로 INSERT.
  B. slot의 행이 있고 effective_at <= now (이미 시행됨) 이며
     name/content/is_required가 이번 seed와 완전히 동일함 (예: 이
     migration이 이미 한 번 적용된 재실행)
     -> 아무 것도 하지 않는다(중복 INSERT 방지, idempotent).
  C. slot의 행이 있고 effective_at <= now (이미 시행됨) 이며 내용이
     다름 (예: DEV DB에 수동으로 입력된 기존 값)
     -> 기존 행은 삭제/수정하지 않고 expired_at만 EFFECTIVE_AT으로
        설정해 보존한다(과거 행을 참조하는 member_agreement.terms_id
        이력이 깨지지 않도록).
     -> 이번 baseline 약관을 새 row로 INSERT.
  D. slot의 행이 있고 (effective_at > now, 즉 아직 시행되지 않은
     미래 예약 약관) 이거나 (effective_at >= _EFFECTIVE_AT, 즉 이
     행을 EFFECTIVE_AT으로 expire시키면 ck_terms_effective_period
     CHECK를 위반하게 되는 경우)
     -> 이 code는 완전히 건너뛴다. 그 행을 expire/삭제하지 않고,
        baseline도 INSERT하지 않는다. index가 code당 하나의 slot만
        허용하므로 그 행을 보존하면서 동시에 baseline을 그 slot에
        넣을 방법이 없다 — 실제로 예정된 약관 개정을 데모용
        baseline이 밀어내지 않도록, 이 경우엔 아무 것도 하지 않는
        쪽을 선택한다.

C 케이스는 반드시 기존 행을 먼저 expire(UPDATE)한 뒤에 새 행을
INSERT하는 순서를 지킨다 — 순서를 바꾸면 두 active row가 순간적으로
공존하려다 unique violation이 난다.

deleted_at이 설정된 과거 row, 이미 expired_at이 있는 과거 row는 전혀
건드리지 않는다.

downgrade는 "데이터 삭제보다 이력 보존을 우선한다"는 원칙에 따라
제한적으로 구현한다. 자세한 이유는 downgrade() 함수 docstring 참고.
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'aa3c28032296'
down_revision: Union[str, None] = '9b41c7d2e5f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 이번 baseline 약관 3종의 공통 적용 시점. 항상 이 고정된 UTC
# timestamp를 쓰기 때문에(요청된 기준 날짜 2026-08-29), migration이
# 여러 환경(DEV/시연)에 적용되어도 effective_at 값이 달라지지 않고,
# downgrade에서 "이 migration이 만든 row"를 code + effective_at 조합으로
# 결정론적으로 식별할 수 있다.
_EFFECTIVE_AT = datetime(2026, 8, 29, 0, 0, 0, tzinfo=timezone.utc)

_TERMS_OF_SERVICE_CONTENT = """제1조 (목적)

본 약관은 Don't Paw-get Your Book(이하 "서비스")이 제공하는 도서 관리, 버츄얼 서재, 도서 정보 확인, OCR 및 AI 기반 추천·분석 기능 등 서비스의 이용과 관련하여 서비스와 이용자 간의 기본적인 사항을 정하는 것을 목적으로 합니다.


제2조 (서비스의 제공)

서비스는 이용자에게 다음과 같은 기능을 제공할 수 있습니다.

1. 회원가입 및 회원정보 관리
2. 도서 검색 및 서재 등록·관리
3. 도서 이미지 및 문장 이미지의 OCR 분석
4. 도서 정보, 독서 기록 및 스크랩 관리
5. 이용자가 선택하거나 제공한 정보를 활용한 AI 기반 도서 추천 및 분석
6. 그 밖에 프로젝트의 목적에 따라 제공되는 관련 기능

서비스의 구체적인 기능과 제공 방식은 개발 및 운영 과정에서 변경될 수 있습니다.


제3조 (회원가입 및 계정 관리)

이용자는 서비스가 정한 절차에 따라 회원가입을 신청할 수 있습니다.

이용자는 자신의 계정 정보를 정확하게 관리해야 하며, 타인의 정보를 도용하거나 다른 사람의 계정을 부정하게 사용해서는 안 됩니다.

서비스의 정상적인 운영 또는 보안을 위해 필요한 경우 특정 계정의 이용이 제한될 수 있습니다.


제4조 (이용자의 의무)

이용자는 다음 행위를 해서는 안 됩니다.

1. 다른 이용자의 계정 또는 개인정보를 무단으로 이용하는 행위
2. 서비스의 시스템 또는 네트워크에 비정상적으로 접근하는 행위
3. 서비스 운영을 고의로 방해하는 행위
4. 타인의 저작권, 개인정보 또는 기타 권리를 침해하는 행위
5. 관련 법령 또는 공공질서에 위반되는 목적으로 서비스를 이용하는 행위


제5조 (OCR 및 AI 기능)

서비스에서 제공하는 OCR, 도서 추천 및 AI 기반 분석 결과는 자동화된 기술을 이용하여 생성될 수 있습니다.

OCR 또는 AI 분석 결과는 원본 이미지의 품질, 입력 정보, 외부 데이터 및 기술적 한계 등에 따라 실제 내용과 다르거나 부정확할 수 있습니다.

이용자는 중요한 정보의 경우 원본 도서 또는 다른 신뢰할 수 있는 자료를 통해 내용을 직접 확인해야 합니다.


제6조 (서비스의 변경 및 중단)

서비스는 개발, 점검, 시스템 장애, 외부 서비스 변경 또는 기타 운영상 필요한 사유가 있는 경우 서비스의 일부 또는 전부를 변경하거나 일시적으로 중단할 수 있습니다.

프로젝트 및 시연 환경의 특성상 일부 기능은 예고 없이 변경될 수 있습니다.


제7조 (게시물 및 이용자 입력 정보)

이용자가 서비스에 입력하거나 등록한 도서 정보, 스크랩, 이미지 및 기타 데이터의 권리는 해당 권리자에게 있습니다.

이용자는 서비스를 이용하면서 타인의 저작권 또는 기타 권리를 침해하는 자료를 무단으로 등록해서는 안 됩니다.


제8조 (서비스 이용 제한)

서비스는 이용자가 본 약관을 위반하거나 시스템의 안정적인 운영을 방해하는 경우 필요한 범위에서 서비스 이용을 제한할 수 있습니다.


제9조 (회원 탈퇴)

이용자는 서비스가 제공하는 회원 탈퇴 기능을 통해 서비스 이용을 종료할 수 있습니다.

회원 탈퇴 시 개인정보 및 이용 데이터는 서비스의 개인정보 처리 정책과 관련 법령에 따라 처리됩니다.


제10조 (약관의 변경)

서비스는 기능 변경 또는 운영 정책 변경 등에 따라 본 약관을 변경할 수 있습니다.

약관이 변경되는 경우 변경된 약관의 적용 시점과 내용을 서비스에서 확인할 수 있도록 제공할 수 있습니다.


제11조 (프로젝트 운영에 관한 안내)

본 서비스는 프로젝트 개발 및 시연 목적으로 운영되는 서비스이며, 상용 서비스와 동일한 수준의 지속적인 제공 또는 완전한 정확성을 보장하지 않습니다.

향후 실제 서비스로 전환되는 경우 본 약관은 운영 정책 및 관련 법령에 맞게 변경될 수 있습니다."""

_PRIVACY_CONTENT = """Don't Paw-get Your Book 서비스는 회원가입 및 서비스 제공을 위해 다음과 같이 개인정보를 수집·이용합니다.


1. 수집하는 개인정보 항목

서비스는 회원가입 과정에서 다음 정보를 처리할 수 있습니다.

- 이메일 주소
- 닉네임
- 생년월일
- 성별
- 서비스 내부 회원 식별자

비밀번호 등 인증에 필요한 정보는 인증 시스템을 통해 처리되며, 서비스의 일반 회원정보 데이터베이스에 평문 비밀번호를 저장하지 않습니다.


2. 개인정보 이용 목적

수집한 개인정보는 다음 목적으로 이용됩니다.

- 회원가입 및 사용자 식별
- 로그인 및 계정 인증
- 회원정보 관리
- 사용자별 서재 및 서비스 데이터 제공
- 서비스 운영 및 오류 대응
- 서비스 기능 개선


3. 개인정보 보유 및 이용 기간

회원정보는 원칙적으로 회원 탈퇴 시까지 보유·이용합니다.

다만 서비스 운영 또는 관련 법령에 따라 일정 기간 보관이 필요한 정보가 있는 경우 해당 목적에 필요한 범위에서 보관될 수 있습니다.


4. 개인정보의 삭제

회원 탈퇴 또는 개인정보 보유 목적이 달성된 경우 서비스는 더 이상 필요하지 않은 개인정보를 삭제하거나 이용할 수 없는 상태로 처리합니다.

다만 서비스의 기술적 구조 및 관계 데이터의 무결성 유지를 위해 일부 식별 정보 또는 처리 이력이 별도로 관리될 수 있습니다.


5. 개인정보 제공에 대한 동의

이용자는 개인정보 수집 및 이용에 대한 동의를 거부할 수 있습니다.

다만 이메일 등 회원가입 및 계정 관리에 필요한 필수 개인정보의 수집 및 이용에 동의하지 않는 경우 회원가입 또는 일부 서비스 이용이 제한될 수 있습니다.


6. 서비스 이용 과정에서 생성되는 정보

서비스 이용 과정에서 도서 등록 정보, 서재 정보, 독서 기록, 스크랩 및 기능 이용 기록 등이 생성될 수 있습니다.

이러한 정보는 해당 기능 제공 및 사용자별 서비스 데이터 관리를 위해 처리될 수 있습니다.


7. 프로젝트 운영에 관한 안내

본 개인정보 수집 및 이용 동의 내용은 프로젝트 및 시연 환경을 기준으로 작성된 기본 정책입니다.

향후 실제 서비스로 전환하거나 개인정보 처리 방식이 변경되는 경우 실제 처리 내용 및 관련 법령에 맞게 내용을 변경할 수 있습니다."""

_AI_ANALYSIS_CONTENT = """Don't Paw-get Your Book 서비스는 도서 추천 및 사용자 맞춤형 기능을 제공하기 위해 AI 기반 분석 기능을 사용할 수 있습니다.


1. AI 분석 기능의 목적

AI 분석 기능은 다음과 같은 목적으로 사용될 수 있습니다.

- 사용자의 관심사 또는 선택 정보를 활용한 도서 추천
- 도서 및 독서 정보의 분류 또는 분석
- 도서 이미지 또는 문장 이미지에서 추출된 정보의 분석
- 서비스 내 사용자 맞춤형 기능 제공


2. AI 분석에 활용될 수 있는 정보

AI 기능 사용 시 다음과 같이 이용자가 직접 제공하거나 서비스 이용 과정에서 생성된 정보가 분석에 활용될 수 있습니다.

- 이용자가 선택하거나 입력한 정보
- 도서 제목, 저자 등 도서 관련 정보
- 이용자가 등록한 도서 및 서재 정보
- OCR을 통해 추출된 도서 또는 문장의 텍스트
- 서비스 기능 이용 과정에서 생성된 분석 대상 정보

AI 분석에는 해당 기능 제공에 필요한 범위의 정보만 사용합니다.


3. AI 분석 결과의 한계

AI 분석 및 추천 결과는 자동화된 기술에 의해 생성될 수 있으며 항상 정확하거나 완전한 결과를 보장하지 않습니다.

도서 정보, OCR 결과, 추천 결과 및 기타 분석 결과에는 오류가 포함될 수 있으므로 필요한 경우 이용자가 직접 내용을 확인해야 합니다.


4. 선택 동의

AI 분석 기능 이용에 대한 동의는 선택 사항입니다.

AI 분석에 동의하지 않더라도 회원가입과 기본적인 서비스 이용은 가능합니다.

다만 AI 분석을 기반으로 제공되는 추천 또는 일부 맞춤형 기능은 제한될 수 있습니다.


5. 프로젝트 운영에 관한 안내

본 동의 내용은 프로젝트 및 시연 환경에서 제공되는 AI 기능을 기준으로 작성되었습니다.

향후 실제 서비스의 AI 처리 방식 또는 기능이 변경되는 경우 실제 처리 내용에 맞게 본 동의 내용도 변경될 수 있습니다."""

_BASELINE_TERMS = (
    {
        "code": "TERMS_OF_SERVICE",
        "name": "서비스 이용약관",
        "content": _TERMS_OF_SERVICE_CONTENT,
        "is_required": True,
    },
    {
        "code": "PRIVACY",
        "name": "개인정보 수집 및 이용 동의",
        "content": _PRIVACY_CONTENT,
        "is_required": True,
    },
    {
        "code": "AI_ANALYSIS",
        "name": "AI 분석 기능 이용 동의",
        "content": _AI_ANALYSIS_CONTENT,
        "is_required": False,
    },
)


def _terms_table() -> sa.Table:
    # 실제 app/models/terms.py를 import하지 않고, migration 시점의
    # 스키마를 로컬에 고정 정의한다(다른 migration 파일들의 관례와
    # 동일 — 향후 모델이 바뀌어도 이 migration의 동작이 바뀌지 않도록).
    metadata = sa.MetaData()
    return sa.Table(
        "terms",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(length=50)),
        sa.Column("name", sa.String(length=100)),
        sa.Column("content", sa.Text()),
        sa.Column("is_required", sa.Boolean()),
        sa.Column("effective_at", sa.DateTime(timezone=True)),
        sa.Column("expired_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )


def upgrade() -> None:
    connection = op.get_bind()
    terms = _terms_table()

    now = datetime.now(timezone.utc)

    for seed in _BASELINE_TERMS:
        code = seed["code"]

        # partial unique index 기준의 "이 code의 유일한 slot"을 이
        # migration이 안전하게 expire할 수 없는 행이 차지하고 있는지
        # 먼저 SQL 비교로 확인한다(Python에서 fetch한 값과 datetime을
        # 직접 비교하지 않는다 — app/repositories/terms_repository.py의
        # 기존 관례와 동일하게 비교는 SQL 쪽에서 수행한다. DB 드라이버에
        # 따라 datetime의 tzinfo 보존 여부가 달라질 수 있어, Python에서
        # 비교하면 aware/naive mismatch로 깨질 수 있다).
        #
        # 두 조건 중 하나라도 해당하면 이 code는 완전히 건너뛴다(D):
        #   - effective_at > now: 아직 시행되지 않은 미래 예약 약관.
        #     실제로 시행되기도 전에 expire시키면 안 된다.
        #   - effective_at >= _EFFECTIVE_AT: 이 조건이 없으면(예: 이
        #     migration이 2026-08-29 이후 어느 시점에 실행되어 그
        #     사이 effective_at이 _EFFECTIVE_AT 이상인 다른 행이 이미
        #     시행된 경우) 아래 C 케이스에서 expired_at=_EFFECTIVE_AT로
        #     UPDATE하면 그 행 자신의 effective_at보다 작거나 같은
        #     expired_at이 되어 ck_terms_effective_period(expired_at >
        #     effective_at) CHECK를 위반한다.
        blocking_row_exists = (
            connection.execute(
                sa.select(terms.c.id)
                .where(
                    terms.c.code == code,
                    terms.c.deleted_at.is_(None),
                    terms.c.expired_at.is_(None),
                    sa.or_(
                        terms.c.effective_at > now,
                        terms.c.effective_at >= _EFFECTIVE_AT,
                    ),
                )
                .limit(1)
            ).first()
            is not None
        )
        if blocking_row_exists:
            continue

        # 여기 도달했다면 이 code의 slot에 남을 수 있는 행은 없거나,
        # 있어도 effective_at < _EFFECTIVE_AT (그리고 <= now) 인 이미
        # 시행된 행뿐이다.
        active_row = connection.execute(
            sa.select(
                terms.c.id, terms.c.name, terms.c.content, terms.c.is_required
            ).where(
                terms.c.code == code,
                terms.c.deleted_at.is_(None),
                terms.c.expired_at.is_(None),
            )
        ).first()

        if active_row is not None:
            same_content = (
                active_row.name == seed["name"]
                and active_row.content == seed["content"]
                and active_row.is_required == seed["is_required"]
            )
            if same_content:
                # B. 이미 동일한 baseline이 seed되어 있음(재실행) — skip.
                continue

            # C. 기존 active row는 보존하고 expire만 시킨다. 위 D
            # 분기에서 effective_at < _EFFECTIVE_AT임을 이미 확인했으므로
            # ck_terms_effective_period(expired_at > effective_at)를
            # 만족한다.
            connection.execute(
                terms.update()
                .where(terms.c.id == active_row.id)
                .values(expired_at=_EFFECTIVE_AT, updated_at=sa.text("now()"))
            )

        # A/C. 새 baseline 행을 INSERT한다. id는 Identity 컬럼이므로
        # 지정하지 않고 DB가 생성하게 둔다.
        connection.execute(
            terms.insert().values(
                code=code,
                name=seed["name"],
                content=seed["content"],
                is_required=seed["is_required"],
                effective_at=_EFFECTIVE_AT,
                expired_at=None,
                deleted_at=None,
            )
        )


def downgrade() -> None:
    """
    이 downgrade는 의도적으로 제한적이다(데이터 삭제보다 이력 보존을
    우선).

    이 migration이 INSERT한 행은 (code, effective_at == _EFFECTIVE_AT,
    name, content, is_required가 seed 값과 정확히 일치)로 결정론적으로
    식별한다. 다만:

    - 그 행을 참조하는 member_agreement가 이미 존재하면(= 그 사이 누군가
      이 baseline 약관에 실제로 동의함) 삭제하지 않는다. 삭제하면
      member_agreement.terms_id FK가 끊어지거나, 존재하지 않는 약관에
      동의했던 것처럼 이력이 소실된다. 이 경우 RuntimeError로 명확히
      실패시켜 운영자가 직접 판단하게 한다(9b41c7d2e5f3의 PENDING enum
      downgrade와 동일한 원칙).
    - 참조가 없으면 그 행을 삭제하고, 이 migration이 C 케이스로 expire
      시켰던 이전 active row가 있다면(같은 code에서 expired_at ==
      _EFFECTIVE_AT인 행) 다시 active(expired_at = NULL)로 복구한다 —
      단, 복구 시점에 해당 code의 active row가 이미 없어야 partial
      unique index와 충돌하지 않으므로, 그 조건을 먼저 확인한다.
    """
    connection = op.get_bind()
    terms = _terms_table()

    for seed in _BASELINE_TERMS:
        code = seed["code"]

        inserted_row = connection.execute(
            sa.select(terms.c.id).where(
                terms.c.code == code,
                terms.c.effective_at == _EFFECTIVE_AT,
                terms.c.name == seed["name"],
                terms.c.content == seed["content"],
                terms.c.is_required == seed["is_required"],
            )
        ).first()

        if inserted_row is None:
            # 이 migration이 이 code에 대해 아무 row도 추가하지
            # 않았다(재실행으로 skip되었던 경우) — downgrade도 아무
            # 것도 하지 않는다.
            continue

        inserted_id = inserted_row.id

        agreement_count = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM member_agreement WHERE terms_id = :terms_id"
            ),
            {"terms_id": inserted_id},
        ).scalar_one()

        if agreement_count:
            raise RuntimeError(
                f"Cannot downgrade: terms.id={inserted_id} (code={code!r}) "
                f"seeded by migration {revision} is referenced by "
                f"{agreement_count} member_agreement row(s). Deleting it "
                "would break agreement history. Resolve manually before "
                "downgrading."
            )

        connection.execute(terms.delete().where(terms.c.id == inserted_id))

        expired_by_this_migration = connection.execute(
            sa.select(terms.c.id).where(
                terms.c.code == code,
                terms.c.expired_at == _EFFECTIVE_AT,
                terms.c.deleted_at.is_(None),
            )
        ).first()

        if expired_by_this_migration is None:
            continue

        still_active_exists = connection.execute(
            sa.select(terms.c.id).where(
                terms.c.code == code,
                terms.c.deleted_at.is_(None),
                terms.c.expired_at.is_(None),
            )
        ).first()

        if still_active_exists is not None:
            # 예상치 못하게 이미 다른 active row가 있다면(수동 개입 등)
            # 복구 시 unique index와 충돌하므로 복구를 건너뛴다.
            continue

        connection.execute(
            terms.update()
            .where(terms.c.id == expired_by_this_migration.id)
            .values(expired_at=None, updated_at=sa.text("now()"))
        )
