# 상용(prod) NodePool 격리 작업 정리

`backend-auth` 상용 파드를 **auth 전용 노드**에만 배치하고, 다른 서비스 파드가
그 노드에 끼어들지 못하게 막는 작업입니다. EKS **Auto Mode(Karpenter)** 환경 기준.

- 클러스터: `dpyb-prod` (dev 클러스터 `dpyb-dev` 는 노드 분리 **안 함**)
- 네임스페이스: `dpyb-auth`
- 관련 파일
  - `k8s/cluster/nodepool-auth.yaml` — auth 전용 NodePool (클러스터 1회 apply)
  - `k8s/overlays/prod/nodepin-patch.yaml` — Deployment 에 nodeSelector + toleration 주입
  - `k8s/overlays/prod/kustomization.yaml` — 위 patch 를 prod overlay 에 연결

## 격리 원리 (label + taint)

| 요소 | 위치 | 역할 |
| --- | --- | --- |
| `nodeSelector: workload=auth` | 파드(Deployment) | backend-auth 를 **auth 노드로 보냄** |
| `label workload=auth` | 노드(NodePool) | nodeSelector 가 고를 대상 |
| `taint dedicated=auth:NoSchedule` | 노드(NodePool) | **다른 서비스 파드 차단** |
| `toleration dedicated=auth` | 파드(Deployment) | backend-auth 만 taint 통과 허용 |

> label 만 있으면 "backend-auth 는 auth 노드로 간다"만 보장됨.
> 다른 서비스가 auth 노드로 끼어드는 건 **taint 로 막는다** → label + taint 조합이 필요.

## 작업 순서

### 0. 사전 확인 — prod 컨텍스트인지 반드시 체크

```bash
# 현재 컨텍스트가 dpyb-prod 인지 확인 (dev 에 실수로 적용 방지)
kubectl config current-context

# 아니면 prod 로 전환
aws eks update-kubeconfig --name dpyb-prod --region ap-northeast-2
```

### 1. NodePool 생성 (클러스터에 1회)

```bash
kubectl apply -f k8s/cluster/nodepool-auth.yaml

# 생성 확인
kubectl get nodepool auth -o wide
kubectl describe nodepool auth
```

> ⚠️ Auto Mode 는 `karpenter.sh/v1` NodePool + `eks.amazonaws.com` 그룹의
> 내장 `default` NodeClass 를 사용합니다. 오픈소스 Karpenter 의 `EC2NodeClass`
> 예제와 다르니 복붙 주의. 노드는 미리 만들지 않고 파드가 뜰 때 Karpenter 가
> 자동 프로비저닝합니다. (상한: `limits.cpu: "10"`)

### 2. prod overlay 에 nodepin-patch 연결

`k8s/overlays/prod/kustomization.yaml` 의 `patches:` 에 이미 포함되어 있음:

```yaml
patches:
  - path: configmap-patch.yaml
  - path: nodepin-patch.yaml   # ← auth 노드 고정
```

로컬에서 렌더 결과 검증(적용 전 확인):

```bash
# nodeSelector / tolerations 가 잘 주입됐는지 확인
kubectl kustomize k8s/overlays/prod | grep -A4 -E 'nodeSelector|tolerations'
```

### 3. 배포 (GitOps — ArgoCD)

직접 `kubectl apply` 하지 않고 `main` 에 머지하면 ArgoCD 가 반영합니다.

```bash
# nodepin-patch 연결 커밋을 main 에 반영
git add k8s/overlays/prod/kustomization.yaml k8s/overlays/prod/nodepin-patch.yaml
git commit -m "feat(k8s): pin prod backend-auth to dedicated auth nodepool"
git push origin main

# ArgoCD 동기화(자동이 아니면 수동)
argocd app sync backend-auth-prod
```

## 검증

```bash
# 1) 파드가 auth 노드에 떴는지
kubectl -n dpyb-auth get pods -o wide

# 2) 그 노드에 label/taint 가 붙었는지
NODE=$(kubectl -n dpyb-auth get pod -l app.kubernetes.io/name=backend-auth \
  -o jsonpath='{.items[0].spec.nodeName}')
kubectl get node "$NODE" --show-labels | grep workload=auth
kubectl describe node "$NODE" | grep -A2 Taints

# 3) 다른 서비스 파드가 이 노드에 없는지 (backend-auth 만 있어야 함)
kubectl get pods -A --field-selector spec.nodeName="$NODE" -o wide

# 4) Karpenter 이벤트 (프로비저닝/스케줄 실패 원인 추적)
kubectl get events -n dpyb-auth --sort-by=.lastTimestamp | tail -20
```

## 트러블슈팅

| 증상 | 원인 | 확인/조치 |
| --- | --- | --- |
| 파드 `Pending` | 매칭 노드 없음 + NodePool 미적용 or limits 초과 | `kubectl describe pod` 의 Events, `kubectl describe nodepool auth` |
| 파드가 일반 노드에 뜸 | nodeSelector 미주입 | `kubectl kustomize k8s/overlays/prod \| grep nodeSelector` |
| 다른 서비스가 auth 노드에 끼어듦 | taint 누락 | `kubectl describe node <NODE> \| grep Taints` |
| 노드가 안 뜸 | 잘못된 컨텍스트(dev)에 apply | `kubectl config current-context` 재확인 |

## 롤백

```bash
# overlay 에서 nodepin-patch 제거 후 재배포하면 격리 해제 (일반 노드로 복귀)
# NodePool 자체 제거는 auth 노드가 모두 비워진 뒤 수행
kubectl delete -f k8s/cluster/nodepool-auth.yaml
```
