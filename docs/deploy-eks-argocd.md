# EKS + ArgoCD 배포 가이드

이 문서는 `backend-auth` (FastAPI) 서비스를 **AWS EKS** 에 **ArgoCD(GitOps)** 로 배포하는 방법을 정리합니다.
현재는 **개발(dev) 환경만** 사용하며, 상용(prod) 관련 내용은 주석으로 남겨두었습니다.

## 환경 구성

| 환경 | 브랜치 | 네임스페이스 | overlay | ArgoCD App |
| --- | --- | --- | --- | --- |
| 개발 | `develop` | `dpyb-auth-dev` | `k8s/overlays/dev` | `backend-auth-dev` |
<!-- prod 사용 시 아래 행 주석 해제
| 상용 | `main` | `dpyb-auth` | `k8s/overlays/prod` | `backend-auth-prod` |
-->

> 매니페스트는 **Kustomize base + overlay** 구조라 공통 부분은 한 벌만 관리하고,
> 환경별로 다른 값(레플리카·APP_ENV·Cognito·이미지 태그)만 overlay 에서 덮어씁니다.

## 큰 그림

```
  develop 브랜치 push
        │
        ▼
[GitHub Actions] 이미지 빌드→ECR 푸시→dev overlay 태그 갱신 커밋
        │
        ▼
[Git] k8s/overlays/dev
        │  (ArgoCD 감시)
        ▼
[EKS] dpyb-auth-dev 네임스페이스
```

즉 **사람이 `kubectl apply` 를 직접 하지 않고**, `develop` 에 올리면 ArgoCD 가 자동 반영합니다.
(prod 사용 시 `main` → `dpyb-auth` 네임스페이스로 같은 흐름이 하나 더 추가됩니다.)

## 파일 구조

```
Dockerfile / .dockerignore          # FastAPI 앱 컨테이너 이미지
k8s/
  base/                             # 공통 매니페스트 (네임스페이스·이미지태그 없음)
    kustomization.yaml
    configmap.yaml                  # 비민감 설정 (APP_HOST/PORT, AWS_REGION 등)
    deployment.yaml                 # 앱 실행 (마이그레이션 initContainer + /health 프로브)
    service.yaml                    # ClusterIP Service
    ingress.yaml                    # ALB Ingress (외부 노출)
  overlays/
    dev/                            # namespace dpyb-auth-dev, replicas 1, APP_ENV=development
    prod/                           # (주석 상태) namespace dpyb-auth, replicas 2, APP_ENV=production
  cluster/
    ingressclass-alb.yaml           # EKS Auto Mode ALB IngressClass (클러스터 전역, 1회 적용)
  secret.example.yaml               # 비밀값 생성 "예시" (실제 값은 Git 에 안 올림)
argocd/
  application-dev.yaml              # develop → dev
  application-prod.yaml             # (주석 상태) main → prod
.github/workflows/build-push-ecr.yml # develop 이미지 빌드/푸시 + dev overlay 태그 갱신
```

## 사전 준비 (한 번만)

1. **EKS 클러스터** 가 있고 `kubectl` 접속이 됨
2. **ECR 리포지토리** 생성
   ```bash
   aws ecr create-repository --repository-name backend-auth --region ap-northeast-2
   ```
3. **EKS Auto Mode 사용** (콘솔 "빠른 구성"으로 클러스터 생성 시)
   - 노드/스토리지/로드밸런서 컨트롤러를 AWS 가 자동 관리 → **ALB 컨트롤러 별도 설치 불필요** ✅
   - 단, ALB Ingress 를 쓰려면 **IngressClass 를 한 번 적용**해야 합니다:
     ```bash
     kubectl apply -f k8s/cluster/ingressclass-alb.yaml
     ```
   - Auto Mode 는 서브넷 태그로 public/private 를 구분합니다. `eksctl` 로 만들면 자동 태깅되며,
     콘솔로 만든 경우 서브넷 태그가 없으면 ALB 가 안 뜰 수 있으니 확인하세요.
4. **ArgoCD** 설치
   ```bash
   kubectl create namespace argocd
   kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
   ```
5. **GitHub Actions 용 IAM 역할(OIDC)** 만들고 리포지토리 Variables 에 `AWS_GHA_ROLE_ARN` 등록
   - ECR 푸시 권한 필요

## 채워야 하는 placeholder

- `k8s/overlays/dev/kustomization.yaml`
  → `<ACCOUNT_ID>` (ECR 이미지 경로). `newTag` 는 최초값이며 이후 CI 가 자동 갱신
- `k8s/overlays/dev/configmap-patch.yaml`
  → `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`
- `k8s/base/configmap.yaml` → `AWS_REGION` (공통)
- `k8s/base/ingress.yaml` → (선택) 도메인 `host`
- `k8s/cluster/ingressclass-alb.yaml` → (선택) `certificateARNs` (HTTPS 인증서), `scheme`
<!-- prod 사용 시: k8s/overlays/prod/kustomization.yaml, configmap-patch.yaml 의 값도 채우세요. -->

## 배포 순서

```bash
# 1) 비밀값(Secret) 은 Git 이 아니라 네임스페이스에 직접 생성
kubectl create namespace dpyb-auth-dev
kubectl create secret generic backend-auth-secret \
  --namespace dpyb-auth-dev \
  --from-literal=DATABASE_URL='postgresql+psycopg://user:pass@dev-rds-host:5432/dpyb_auth'

# 2) ArgoCD Application 등록 (이후는 GitOps 자동)
kubectl apply -f argocd/application-dev.yaml

# 3) 동기화 확인
kubectl get applications -n argocd
kubectl get pods,svc,ingress -n dpyb-auth-dev
```

<!-- prod 사용 시
kubectl create namespace dpyb-auth
kubectl create secret generic backend-auth-secret \
  --namespace dpyb-auth \
  --from-literal=DATABASE_URL='postgresql+psycopg://user:pass@prod-rds-host:5432/dpyb_auth'
kubectl apply -f argocd/application-prod.yaml
kubectl get pods,svc,ingress -n dpyb-auth
-->

이후 `develop` 에 머지하면
→ CI 이미지 빌드/푸시 + dev overlay 태그 갱신 커밋 → ArgoCD 자동 배포됩니다.

## 이미지 아키텍처 (멀티아키 빌드)

CI 는 이미지를 **linux/amd64 + linux/arm64 두 아키텍처로** 빌드해 push 합니다
(`.github/workflows/build-push-ecr.yml` 의 QEMU/Buildx 스텝과 `platforms`).

이유는 노드 아키텍처가 고정돼 있지 않기 때문입니다. prod 클러스터는 노드가 전부
arm64(Graviton, 예: `t4g.medium` / `c6g.large`) 이고 dev 는 amd64/arm64 가 섞여
있습니다. `k8s/cluster/nodepool-auth.yaml` 의 NodePool 에는 `kubernetes.io/arch`
제약이 없고 `instance-category: ["t","m"]` 은 Graviton 계열과 x86 계열을 모두
포함하므로, Karpenter 가 어느 쪽 노드든 띄울 수 있습니다.

단일 아키 이미지를 배포하면 **이미지 pull 은 성공하고 실행 시점에 죽습니다.**
증상은 initContainer 부터 나타납니다.

```
Init:CrashLoopBackOff
$ kubectl logs <pod> -c migrate
exec /usr/local/bin/alembic: exec format error
```

`ImagePullBackOff` 와 헷갈리기 쉬운데 원인이 전혀 다릅니다 — 그쪽은 보통 태그가
실제로 없는 경우입니다(예: prod overlay 의 `newTag` 가 `placeholder-set-by-ci`
인 채로 CI 가 한 번도 안 돈 상태).

### 병합 후 확인

`develop` 병합으로 dev CI 를 먼저 통과시킨 뒤 `main` 을 병합하세요. prod ECR 은
IMMUTABLE 이라 새 병합 커밋 SHA 로만 나갑니다.

```bash
# 매니페스트에 아키텍처가 2개인지 — manifest.list 또는 OCI index 에
# linux/amd64, linux/arm64 두 항목이 보이면 성공
aws ecr batch-get-image --region ap-northeast-2   --repository-name dpyb-prod/dpyb-auth   --image-ids imageTag=<새SHA>   --query 'images[0].imageManifest' --output text

kubectl --context dpyb-prod -n dpyb-auth get pods
kubectl --context dpyb-prod -n dpyb-auth logs -l app.kubernetes.io/name=backend-auth -c migrate
```

`migrate` 가 통과하면 그다음 관문은 DB 입니다. initContainer 가 `alembic upgrade
head` 를 돌리므로 prod Secret 의 `DATABASE_URL` 이 올바른 데이터베이스를 가리켜야
합니다(`docs/db-per-service.md` 참고 — 서비스별 DB 분리로 이름이 `dpyb_auth` 입니다).

## 로컬에서 렌더링/이미지 검증

```bash
# Kustomize 결과 미리보기 (클러스터 없이 가능)
kubectl kustomize k8s/overlays/dev
# kubectl kustomize k8s/overlays/prod   # prod 사용 시

# Dockerfile 검증
docker build -t backend-auth:local .
docker run --rm -p 8000:8000 \
  -e DATABASE_URL='postgresql+psycopg://...' \
  -e AWS_REGION=ap-northeast-2 \
  -e COGNITO_USER_POOL_ID=... \
  -e COGNITO_CLIENT_ID=... \
  backend-auth:local
curl localhost:8000/health   # {"status":"ok"}
```

## 참고: Secret 을 Git 으로 관리하고 싶다면

평문 Secret 은 절대 커밋하지 말고, 아래 중 하나를 사용하세요.

- **SealedSecrets** — `kubeseal` 로 암호화한 SealedSecret 을 커밋
- **External Secrets Operator** — AWS Secrets Manager / SSM Parameter Store 와 연동
