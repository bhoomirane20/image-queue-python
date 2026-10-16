#!/usr/bin/env bash
set -e

# Usage: ./k8s/setup.sh <dockerhub-username> <dockerhub-token>
DOCKER_USER="${1:-$DOCKERHUB_USER}"
DOCKER_TOKEN="${2:-$DOCKERHUB_TOKEN}"

if [ -z "$DOCKER_USER" ] || [ -z "$DOCKER_TOKEN" ]; then
  echo "Usage: $0 <dockerhub-username> <dockerhub-token>"
  echo "Or set DOCKERHUB_USER and DOCKERHUB_TOKEN environment variables."
  exit 1
fi

echo "====================================================="
echo " Setting up Kubernetes for image-queue-python"
echo " Docker Hub User: ${DOCKER_USER}"
echo "====================================================="

# 1. Create namespaces
echo "[1/4] Creating namespaces (qa, stage, prod)..."
kubectl apply -f k8s/namespaces.yaml

# 2. Create docker-registry secret in all 3 namespaces
echo "[2/4] Configuring Docker Hub pull secrets (dockerhub)..."
for ns in qa stage prod; do
  kubectl create secret docker-registry dockerhub \
    --docker-username="${DOCKER_USER}" \
    --docker-password="${DOCKER_TOKEN}" \
    --namespace="${ns}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "  ✓ Secret 'dockerhub' ready in namespace '${ns}'"
done

# 3. Apply manifests to each namespace
echo "[3/4] Applying manifests to each namespace..."
# Update image references if username differs from bhoomirane20
if [ "$DOCKER_USER" != "bhoomirane20" ]; then
  sed -i "s|image: bhoomirane20/|image: ${DOCKER_USER}/|g" k8s/qa/*.yaml k8s/stage/*.yaml k8s/prod/*.yaml
fi

kubectl apply -f k8s/qa/
kubectl apply -f k8s/stage/
kubectl apply -f k8s/prod/

echo "[4/4] Waiting for PostgreSQL and Redis to be ready in QA..."
kubectl rollout status deployment/postgres -n qa --timeout=60s || true
kubectl rollout status deployment/redis -n qa --timeout=60s || true
kubectl rollout status deployment/web -n qa --timeout=60s || true
kubectl rollout status deployment/worker -n qa --timeout=60s || true

echo "====================================================="
echo " Kubernetes environment successfully initialized!"
echo ""
echo "Verify pods:"
echo "  kubectl get pods -n qa"
echo "  kubectl get pods -n stage"
echo "  kubectl get pods -n prod"
echo ""
echo "Demonstrate rollout restart in QA:"
echo "  kubectl rollout restart deployment/web -n qa"
echo "  kubectl rollout status deployment/web -n qa"
echo ""
echo "Verify running image and digest:"
echo '  kubectl get pods -n qa -o jsonpath='"'"'{range .items[*]}{.spec.containers[*].image}{"\n"}{end}'"'"
echo '  kubectl describe pod $(kubectl get pods -n qa -l app=web -o jsonpath="{.items[0].metadata.name}") -n qa | grep -i "image id"'
echo "====================================================="
