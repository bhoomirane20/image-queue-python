#!/usr/bin/env bash
set -e

# Usage: ./k8s/deploy-all.sh <dockerhub-username> <dockerhub-token>
USER="${1:-$DOCKERHUB_USER}"
TOKEN="${2:-$DOCKERHUB_TOKEN}"

if [ -z "$USER" ] || [ -z "$TOKEN" ]; then
  echo "Usage: $0 <dockerhub-username> <dockerhub-token>"
  echo "Or set DOCKERHUB_USER and DOCKERHUB_TOKEN environment variables."
  exit 1
fi

export USER

echo "====================================================="
echo " Deploying image-queue-python across qa, stage, prod"
echo " Docker Hub User: $USER"
echo "====================================================="

# Step 2: Create the three namespaces
echo "[Step 2] Creating namespaces (qa, stage, prod)..."
for ns in qa stage prod; do
  kubectl create namespace $ns --dry-run=client -o yaml | kubectl apply -f -
done

# Step 3: Create Docker Hub pull secret in each namespace
echo "[Step 3] Creating Docker Hub pull secrets..."
for ns in qa stage prod; do
  kubectl create secret docker-registry dockerhub \
    --docker-username="$USER" \
    --docker-password="$TOKEN" \
    -n $ns --dry-run=client -o yaml | kubectl apply -f -
done

# Step 4: Create app secret (DB credentials) in each namespace
echo "[Step 4] Creating app database secrets..."
for ns in qa stage prod; do
  kubectl create secret generic app-secrets \
    --from-literal=POSTGRES_USER=app \
    --from-literal=POSTGRES_PASSWORD=postgres \
    --from-literal=POSTGRES_DB=appdb \
    -n $ns --dry-run=client -o yaml | kubectl apply -f -
done

# Step 6: Deploy to each namespace with its own tag
echo "[Step 6] Deploying manifests with respective tags..."
for pair in qa:qa stage:stage prod:prod; do
  ns=${pair%%:*}
  tag=${pair##*:}
  echo "  --> Applying to namespace '$ns' with tag ':$tag'..."
  kubectl apply -f k8s/postgres.yaml -n $ns
  kubectl apply -f k8s/redis.yaml -n $ns
  TAG=$tag envsubst < k8s/app.yaml | kubectl apply -n $ns -f -
done

echo ""
echo "====================================================="
echo " Deployment applied successfully!"
echo " Verifying QA status:"
kubectl get pods,svc -n qa
echo "====================================================="
